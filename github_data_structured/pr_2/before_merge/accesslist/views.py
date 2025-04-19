import json
import os
import re
import sys
import threading
import uuid
from inspect import stack
from time import sleep

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.forms import model_to_dict
from django.http import (
    FileResponse,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotFound,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from drf_yasg.utils import swagger_auto_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_api_key.permissions import HasAPIKey

from accesslist.utils.gitlab import (
    get_templates_files_from_gitlab,
    get_templates_from_gitlab,
)
from ownerlist.models import Iplist, Owners
from ownerlist.utils import (
    BASE_DIR,
    FORM_APPLICATION_KEYS,
    FORM_URLS,
    BaseView,
    ClearSessionMeta,
    GitWorker,
    MakeTemporaryToken,
    ParseDocx,
    SendMessageToApprove,
    acl_sending_retry_checking,
    convert_md_to_dict,
    create_markdown_file,
    get_acl_from_gitlab,
    get_client_ip,
    get_files_from_gitlab,
    get_gitlab_project_info,
    ip_status,
    is_valid_uuid,
    logger,
    make_doc,
    omni_check_status,
    request_handler,
    send_onmitracker,
    send_to_mattermost,
    sync_acl_portal_projects_list,
    update_callback_status,
    upload_file_handler,
)
from teams.models import Block, Team

from .forms import Approve_form
from .models import ACL, ACLGitlabStore

tasklist = ["ACT_MAKE_DOCX", "ACT_MAKE_GIT", "ACT_OMNI"]
jobs = ["docx", "git", "omni"]
# геморрой с выкаткой


class ObjectMixin:
    """Миксин обработки запросов и отобращение страниц"""

    template = None
    url = None

    def get(self, request, acl_id=None):
        context = {}
        tmp = None
        # Очищаем кеш активностей (бага с быстрым выполнением JOB)
        cache.set(acl_id, {})
        if acl_id is None:
            if "uuid" in request.session is not None:
                if request.session["uuid"]:
                    return redirect(
                        reverse(self.url, kwargs={"acl_id": request.session["uuid"]})
                    )

            if "HTTP_REFERER" in request.META.keys():
                if reverse(FORM_URLS[0]) in request.META.get("HTTP_REFERER"):
                    # Заполняем uuid для нового acl
                    ClearSessionMeta()
                    request.session["uuid"] = str(uuid.uuid4())
                    request.session["LOCAL_STORAGE"] = {}
                    request.session["taskid"] = None
                    return HttpResponseRedirect(
                        reverse(
                            FORM_URLS[1], kwargs={"acl_id": request.session["uuid"]}
                        )
                    )
        else:
            if "uuid" in request.session:  # is not None
                if "/new/" in request.path:
                    if str(acl_id) != request.session["uuid"]:
                        return HttpResponseRedirect(reverse(FORM_URLS[0]))
                elif str(acl_id) == request.session["uuid"]:
                    response = reverse(
                        self.url, kwargs={"acl_id": request.session["uuid"]}
                    )  # aclcreate_new_urls
                    # Временный workaround
                    if "new" not in response:
                        response += "new"
                    return redirect(response)

            if "/new/" not in request.path:
                # ClearSessionMeta()
                tmp = get_object_or_404(ACL, id=str(acl_id))

                request.session["LOCAL_STORAGE"] = {
                    "acl_create_info.html": tmp.create_info if tmp.create_info else {},
                    "acl_traffic.html": tmp.traffic_rules if tmp.traffic_rules else {},
                    "acl_external_resources.html": (
                        tmp.external_resources if tmp.external_resources else {}
                    ),
                    "acl_internal_resources.html": (
                        tmp.internal_resources if tmp.internal_resources else {}
                    ),
                    "acl_dmz_resources.html": (
                        tmp.dmz_resources if tmp.dmz_resources else {}
                    ),
                }
                request.session["taskid"] = tmp.taskid or ""

                if request.META.get("HTTP_REFERER", "") == "" and tmp.status in [
                    "WTE",
                    "APRV",
                    "CNL",
                ]:
                    if tmp.approve == request.user or request.user.is_staff:
                        context.update({"acl_owner": tmp.owner})
                        # else:
                        response = HttpResponseRedirect(
                            reverse("acl_pending_urls", kwargs=({"acl_id": acl_id}))
                        )
                        response["Location"] += f"?token={tmp.token}"
                        return response

                    if tmp.approve == request.user:
                        context.update({"debtor": "True", "token": tmp.token})

                context.update({"status": str(tmp.status), "app_person": tmp.approve})

                if tmp.team is not None:
                    context.update({"team_block": tmp.team.included_teams.all()[0]})

            if "traffic" in request.path:
                context.update(
                    {
                        "widgets": [
                            "input__domain_source",
                            "input__ip_source",
                            "input__domain_dest",
                            "input__ip__external",
                            "input__host_port",
                            "input__application_port",
                            "input_descr",
                            "input_is_reserve",
                        ]
                    }
                )
            if "/info/" in request.path:
                if tmp:
                    activity = tmp.activity.split(";")
                    context.update({"ACT": activity, "team": tmp.team})
                    for ACT in activity:
                        if ".git" not in ACT:
                            request.session[ACT] = True
                        # else:
                        #     request.session['GIT_URL'] = ACT
            if "LOCAL_STORAGE" in request.session:
                if "acl_create_info.html" in request.session["LOCAL_STORAGE"]:
                    acl_proj = (
                        request.session["LOCAL_STORAGE"]
                        .get("acl_create_info.html", {})
                        .get("project", "")
                    )
                    acl_git_file = (
                        request.session["LOCAL_STORAGE"]
                        .get("acl_create_info.html", {})
                        .get("file_name", "")
                    )
                    latest_acl_obj = ACL.objects.filter(
                        project=acl_proj,
                        git_filename=acl_git_file,
                        status__in=["JOB", "CMP"],
                    )
                    if len(latest_acl_obj) > 0:
                        latest_acl_obj = latest_acl_obj.latest("created")
                        latest_uuid = latest_acl_obj.id
                        latest_project = latest_acl_obj.project
                        latest_git_file = latest_acl_obj.git_filename
                        context["latest_local_storage"] = latest_acl_obj.acl_data
                        context["latest_uuid"] = latest_uuid
                        context["latest_project"] = latest_project
                        context["latest_git_file"] = latest_git_file
                    else:
                        print("Последнего acl не найдено.")
                context.update(
                    {
                        "acl_id": str(acl_id),
                        "FULL_STORAGE": request.session["LOCAL_STORAGE"],
                        "FORM_APPLICATION_KEYS": FORM_APPLICATION_KEYS,
                        "template_name": self.template,
                        "blocks": Block.objects.all(),
                        "acl_gitlab_store": ACLGitlabStore.objects.all(),
                    }
                )
                if tmp and len(tmp.taskid) >= 3:
                    if re.match(r"\d{4,}", str(tmp.taskid)):
                        context.update({"taskid": str(tmp.taskid)})

                if self.template not in request.session["LOCAL_STORAGE"]:
                    return render(request, self.template, context=context)
                else:
                    context["LOCAL_STORAGE"] = request.session["LOCAL_STORAGE"][
                        self.template
                    ]
                    return render(request, self.template, context=context)

        return HttpResponseRedirect(reverse(FORM_URLS[0]))

    def post(self, request, acl_id=None):
        if acl_id is not None:
            tmp = request_handler(request, self.template)
            current_page = FORM_URLS.index(self.url)
            NO_MAKE_JOB = request.GET.get("request", "yes")
            if tmp:
                try:
                    # Проверка на пустые данные
                    if (
                        len(tmp[self.template]) == 0
                        or len(tmp[self.template][0]) == 0
                        or len(tmp[self.template][0][0]) == 0
                    ):
                        if self.template in request.session["LOCAL_STORAGE"]:
                            del request.session["LOCAL_STORAGE"][self.template]
                    else:
                        # Обновление данных в сессии с учетом ключей
                        if self.template in request.session["LOCAL_STORAGE"]:
                            request.session["LOCAL_STORAGE"].update(
                                {self.template: tmp[self.template]}
                            )
                        else:
                            request.session["LOCAL_STORAGE"][self.template] = tmp[
                                self.template
                            ]
                except Exception:
                    request.session["LOCAL_STORAGE"][self.template] = tmp[self.template]

                request.session.modified = True

                # Существующая логика проверки и сохранения
                if "/new/" in request.path:
                    if "uuid" not in request.session or request.session["uuid"] != str(
                        acl_id
                    ):
                        logger.warning("Попытка записи в чужой uuid")
                        return redirect(
                            reverse(
                                FORM_URLS[current_page + 1], kwargs={"acl_id": acl_id}
                            )
                        )
                    # else:
                    #    return HttpResponseRedirect(reverse(FORM_URLS[0]))
                owner_form = request.session["LOCAL_STORAGE"].get(
                    "acl_create_info.html", {}
                )
                if not owner_form:
                    messages.warning(
                        request,
                        "Для продолжения, необходимо заполнить контактные данные.",
                    )
                    return redirect(f"{reverse(FORM_URLS[1])}{acl_id}/")
                obj = save__form(request, owner_form, acl_id)
                if obj is not None:
                    return obj
                if NO_MAKE_JOB == "no":
                    messages.info(request, "Данные сохранены")
                    return redirect(f"{reverse(FORM_URLS[current_page])}{acl_id}/")
                return redirect(f"{reverse(FORM_URLS[current_page + 1])}{acl_id}/")
        messages.warning(request, "Не все поля заполнены")
        return render(request, self.template, context={"acl_id": acl_id})


class Aclhistory(BaseView, LoginRequiredMixin, View):
    """История запросов"""

    def get(self, request, acl_id=None, acl_status=None, project_name=None):
        ClearSessionMeta(request)
        if acl_id is not None:
            # if request.user.is_staff:
            acllist = ACL.objects.filter(id__exact=acl_id)
            # else:
            # acllist = ACL.objects.filter(id__exact=acl_id, owner__email__iexact=request.user.email)
        elif acl_status is not None:
            acllist = ACL.objects.filter(status__exact=acl_status).order_by(
                "-created", "-pkid"
            )
        elif project_name is not None:
            acllist = ACL.objects.filter(project__icontains=project_name).order_by(
                "-created", "-pkid"
            )
            logger.info("Отсортировал по проекту")
        else:
            if request.user.is_staff:
                acllist = ACL.objects.order_by("-created", "-pkid")
            else:
                acllist = ACL.objects.filter(
                    Q(owner_id=request.user.id) | Q(approve__exact=request.user)
                ).order_by(
                    "-created", "-pkid"
                )  # owner__email__iexact=request.user.email

        paginator = Paginator(acllist, 10)
        page_number = request.GET.get("page", 1)
        page = paginator.get_page(page_number)

        is_paginated = page.has_other_pages()

        prev_url = ""
        if page.has_previous():
            prev_url = f"?page={page.previous_page_number()}"

        next_url = ""
        if page.has_next():
            next_url = f"?page={page.next_page_number()}"

        context = {
            "acl": ACL,
            "acllists": page,
            "is_paginated": is_paginated,
            "next_url": next_url,
            "prev_url": prev_url,
        }
        return render(request, "acl_history.html", context=context)


class AclCreate(BaseView, LoginRequiredMixin, ObjectMixin, View):
    template = "acl_create_info.html"
    url = "aclcreate_urls"


class AclPrepare(BaseView, ObjectMixin, View):
    template = "acl_pending.html"
    url = "acl_prepare_urls"


class AclCreate_internal(BaseView, LoginRequiredMixin, ObjectMixin, View):
    template = "acl_internal_resources.html"
    url = "aclinternal_urls"


class AclCreate_dmz(BaseView, LoginRequiredMixin, ObjectMixin, View):
    template = "acl_dmz_resources.html"
    url = "acldmz_urls"


class AclCreate_external(BaseView, LoginRequiredMixin, ObjectMixin, View):
    template = "acl_external_resources.html"
    url = "aclexternal_urls"


class AclCreate_traffic(BaseView, LoginRequiredMixin, ObjectMixin, View):
    template = "acl_traffic.html"
    url = "acltraffic_urls"


class Acl_approve(BaseView, LoginRequiredMixin, View):
    """Функция для страницы согласования"""

    def get(self, request, acl_id=None):
        context = {}
        actived = request.GET.get("actived", None)
        token = request.GET.get("token", 0)

        if acl_id is None or not is_valid_uuid(acl_id):
            messages.warning(request, "Неправильный запрос")
            return redirect(reverse("acldemo_urls"))
        try:
            tmp = get_object_or_404(ACL, id=str(acl_id))
        except Exception as e:
            logger.error(e)
            messages.warning(request, "Неправильный запрос")
            return redirect(reverse("acldemo_urls"))

        if actived != "true" or tmp.token != token:
            actived = False

        if tmp.status == "WTE" and not actived:
            # messages.warning(request, 'ACL уже ожидает согласование')
            return redirect(reverse("acl_pending_urls", kwargs=({"acl_id": acl_id})))
        elif tmp.status == "APRV":
            return redirect(reverse("aclcreate_urls", kwargs=({"acl_id": acl_id})))

        form = Approve_form()
        if tmp.team is not None:
            APPROVE_OWNER = tmp.team.response.all()
            APPROVE_LIST = tmp.team.response.all()
            # context.update({'PROJECT': tmp.team.name})
        else:
            APPROVE_OWNER = User.objects.filter(groups__name=settings.APPROVE).filter(
                groups__name=tmp.project
            )
            if APPROVE_OWNER:
                # Берем только одного человека для согласования
                APPROVE_OWNER = APPROVE_OWNER[0]
                APPROVE_LIST = User.objects.filter(
                    groups__name=settings.APPROVE
                ).exclude(id__exact=APPROVE_OWNER.id)
            else:
                APPROVE_LIST = User.objects.filter(groups__name=settings.APPROVE)

        if tmp.team is not None:
            context.update({"team": tmp.team.name})
        else:
            team = tmp.create_info.get("department")
            context.update({"team": team})

        context.update(
            {
                "acl_id": str(acl_id),
                "FULL_STORAGE": tmp.acl_data,
                "FORM_APPLICATION_KEYS": FORM_APPLICATION_KEYS,
                "APPROVE_LIST": APPROVE_LIST,
                "APPROVE_OWNER": APPROVE_OWNER,
                "STATUS": tmp.status,
                "REASON": tmp.comment,
                "form": form,
                "PROJECT": tmp.project,
                "blocks": Block.objects.all(),
            }
        )
        # if actived:
        #    context.update({'actived': 'true',
        #                    'url': request.path})

        return render(request, "acl_approve.html", context=context)

    def post(self, request, acl_id=None):
        form = Approve_form(data=request.POST or None)
        actived = request.GET.get("actived", None)
        token = request.GET.get("token", 0)
        user_list = []
        if "approve_person" not in form.data or len(form.data["approve_person"]) == 0:
            messages.error(request, "Нужно выбрать согласующего")
            return HttpResponseRedirect(
                reverse("acl_approve_urls", kwargs=({"acl_id": acl_id}))
            )

        approve_persons = form.data["approve_person"] or ""
        if form.is_valid():
            tmp = get_object_or_404(ACL, id=str(acl_id))
            if tmp.token == "":
                tmp.token = MakeTemporaryToken()
                actived = False
            else:
                if actived and tmp.token == token:
                    actived = True
                    # tmp.token = MakeTemporaryToken()

            users = (form.cleaned_data["approve_person"]).split(";") or []
            if len(users) >= 5:
                messages.error(
                    request, "Слишком много адресатов, максимальное количество: 5"
                )
                return HttpResponseRedirect(
                    reverse("acl_approve_urls", kwargs=({"acl_id": acl_id}))
                )

            for user in users:
                try:
                    user_obj = User.objects.get(username__exact=user) or None
                except User.DoesNotExist:
                    messages.error(
                        request,
                        f"Выбранный пользователь {user} не может согласовать данный ACL",
                    )

                if user_obj:
                    user_list.append(user_obj)
                    try:
                        SendMessageToApprove(
                            acl_id, tmp.owner, user_obj, token=tmp.token
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при отправке email: {e}")

            tmp.status = "WTE"
            tmp.approve.set(user_list)
            tmp.save()

            return redirect(reverse("acl_pending_urls", kwargs=({"acl_id": acl_id})))

        else:
            if len(form.errors.keys()) > 0:
                messages.error(
                    request,
                    f"Произошла ошибка при изменении данных, вероятно невалидные данные в форме: {form.errors.keys()}",
                )
            else:
                messages.error(request, "Ошибка на стороне сервера")
        storage = get_messages(request)
        if storage:
            return redirect(request.get_full_path())
        return HttpResponseRedirect(
            reverse("acl_approve_urls", kwargs=({"acl_id": acl_id}))
        )


class Acl_pending(BaseView, View):
    """Функция вывода информации об согласовании объекта"""

    def get(self, request, acl_id=None):
        if acl_id is None:
            return redirect(reverse(FORM_URLS[1]))
        prepare = request.GET.get("prepare", False)
        token = request.GET.get("token", "")
        context = {}
        tmp = None
        try:
            tmp = get_object_or_404(ACL, id=str(acl_id))
        except Exception:
            if not prepare:
                messages.warning(
                    request, "Такого ACL листа еще нет, но можете его создать"
                )
            else:
                messages.warning(request, "Необходимо сохранить ACL")
            return HttpResponseRedirect(request.META.get("HTTP_REFERER"))
        # except Exception as e:
        #         messages.warning(request, 'Произошла ошибка при выполнении запроса')
        #         return HttpResponseRedirect(reverse('acldemo_urls'))
        context.update({"obj": tmp})

        if len(tmp.approve.all()) == 0:
            tmp.status = "FL"
            tmp.save()
            messages.warning(request, "Мы не смогли найти согласующего")
            return redirect(reverse("acl_approve_urls", kwargs={"acl_id": acl_id}))
        if not prepare:
            if token != tmp.token:
                if request.user not in tmp.approve.all():
                    context.update({"IS_APPROVE": False})
                    # or request.user != tmp.owner:
                    if request.user != tmp.owner and not request.user.is_staff:
                        messages.warning(
                            request,
                            "Вы не можете получить доступ к данному ACL, вероятно истек токен или Вы не авторизованы.",
                        )
                        return redirect(reverse("acldemo_urls"))
            else:
                context.update({"IS_APPROVE": True})
                # messages.warning(request, 'Не валидный токен, попробуйте запросить новый')
                # return redirect(reverse(FORM_URLS[1]))
            if tmp.status == "APRV":
                if request.user == tmp.owner:
                    return redirect(
                        reverse("acloverview_urls", kwargs={"acl_id": acl_id})
                    )
                else:
                    messages.info(request, "Это действие уже кто-то выполнил")
                    return redirect(
                        reverse("aclcreate_urls", kwargs={"acl_id": acl_id})
                    )

            if tmp.status != "WTE":
                messages.warning(
                    request,
                    "ACL вероятно уже согласован, либо редактируется кем-то другим",
                )
                return redirect(reverse("acl_approve_urls", kwargs={"acl_id": acl_id}))

            context.update(
                {
                    "status": str(tmp.status),
                    "APP_PERSON": tmp.approve.all(),
                    "TOKEN": tmp.token,
                }
            )

            context.update({"LOCAL_STORAGE": tmp.acl_data})
        else:
            if "LOCAL_STORAGE" in request.session:
                context.update({"LOCAL_STORAGE": request.session["LOCAL_STORAGE"]})
        context.update({"acl_id": str(acl_id)})
        context.update({"PREPARE": bool(prepare)})

        return render(request, "acl_pending.html", context=context)


class AclDemo(BaseView, View):
    """Страница приветствия"""

    def get(self, request):
        ClearSessionMeta(request)
        return render(request, "acl_demo.html")


class AclInstruction(BaseView, View):
    def get(self, request):
        ClearSessionMeta(request)
        return render(request, "acl_instruction.html")


def ACldefault(request):
    request.session.set_expiry(0)
    return HttpResponseRedirect(reverse("acldemo_urls"))


@transaction.atomic
def save__form(request, owner_form, acl_id) -> None:
    """Сохранение данныех из сесии в БД"""

    if owner_form is None:
        logger.error("[save__form] owner_form is None")
        return

    owner_email = owner_form.get("email")
    if owner_email and owner_email != request.user.email:
        email = request.user.email
    else:
        email = owner_email

    user = Owners.objects.filter(email=email).first()

    if not user:
        user = Owners.objects.create(
            email=email,
            username=owner_form.get("full_name"),
            phone=owner_form.get("phone"),
            active=True,
            department=owner_form.get("department"),
        )
    try:
        ip, _ = Iplist.objects.get_or_create(ipv4=get_client_ip(request))
        ip.owner = user
        ip.save()
        activity = []
        if any([job for job in tasklist if job in request.session]):
            if "ACT_OMNI" in request.session:
                activity.append("ACT_OMNI")
            if "ACT_MAKE_GIT" in request.session:
                activity.append("ACT_MAKE_GIT")

            # if 'GIT_URL' in request.session:
            #         activity.append(request.session['GIT_URL'])

            # if 'GIT_FILENAME' in request.session:
            #     activity.append(request.session['GIT_FILENAME'])

            if "ACT_MAKE_DOCX" in request.session:
                activity.append("ACT_MAKE_DOCX")

        obj, created = ACL.objects.get_or_create(id=str(acl_id))
        if obj:
            # if obj.status in ['WTE', 'APRV']:
            #    messages.warning(request, 'Вы не можете изменить ACL лист находяшийся на согласовании')
            #    return redirect(reverse('aclcreate_urls'),  kwargs=({"acl_id": acl_id}))
            team_obj = int(request.POST.get("teamid", 0))
            if team_obj > 0:
                team = Team.objects.get(id=team_obj)
                if team:
                    obj.team = team
            obj.is_executed = False
            # Не перезаписывать владельца ACL
            if created:
                obj.owner = request.user
            else:
                if obj.owner is None:
                    if request.user is not None:
                        if len(owner_form) > 1 and request.user.email == owner_form.get(
                            "email"
                        ):
                            obj.owner = request.user
                        else:
                            if request.user.is_staff:
                                try:
                                    tmp_owner = request.user
                                    tmp_owner = User.objects.get(
                                        email=owner_form.get("email")
                                    )
                                    obj.owner = tmp_owner
                                except tmp_owner.DoesNotExist:
                                    obj.owner = request.user

            if len(request.session["LOCAL_STORAGE"]) <= 1:
                obj.status = "NOTFL"
                obj.save(update_fields=["status"])
            else:
                if not created and obj.status == "NOTFL":
                    obj.status = "FL"
                    obj.save(update_fields=["status"])

            hash_mappings = {
                "create_info": "acl_create_info.html",
                "traffic_rules": "acl_traffic.html",
                "external_resources": "acl_external_resources.html",
                "internal_resources": "acl_internal_resources.html",
                "dmz_resources": "acl_dmz_resources.html",
            }

            old_hashes = {}
            for attr in hash_mappings:
                if hasattr(obj, attr) and getattr(obj, attr):
                    value = getattr(obj, attr)
                    old_hashes[attr] = hash(json.dumps(value, sort_keys=True))
            new_hashes = {}
            for attr, key in hash_mappings.items():
                if key in request.session["LOCAL_STORAGE"]:
                    new_hashes[attr] = hash(
                        json.dumps(
                            request.session["LOCAL_STORAGE"].get(key, {}),
                            sort_keys=True,
                        )
                    )
            fields_to_update = []
            for attr, new_hash in new_hashes.items():
                if new_hash != old_hashes.get(attr):
                    if obj.status in ["CMP"]:
                        obj.approve.add([])
                        obj.status = "FL"
                        messages.info(
                            request,
                            "Вы изменили согласованные данные, для формирования обращения, необходимо заново согласовать ACL лист",
                        )
                        break
                    elif obj.status in ["WTE"]:
                        messages.warning(
                            request,
                            "Вы не можете изменить ACL лист находяшийся на согласовании",
                        )
                        return redirect(
                            reverse("aclcreate_urls", kwargs={"acl_id": acl_id})
                        )

                    setattr(
                        obj,
                        attr,
                        request.session["LOCAL_STORAGE"].get(hash_mappings[attr]),
                    )
                    fields_to_update.append(attr)

            if len(activity) > 0:
                obj.activity = ";".join(activity)
                fields_to_update.append("activity")

            obj.project = owner_form.get("project")
            obj.git_filename = owner_form.get("file_name")
            fields_to_update.extend(["project", "git_filename", "owner"])
            try:
                department = request.POST.get("department")
                if department != "" or department != "Нет":
                    team = Team.objects.get(name=department)
                    if team:
                        obj.team = team
                        fields_to_update.extend(["team"])
            except Exception as e:
                logger.warning(f"Не удалось сохранить team в БД: {e}")
            if fields_to_update:
                obj.save(update_fields=fields_to_update)

    except Exception as e:
        messages.error(request, f"Ошибка, мы не смогли записать данные в БД. {e}")
        logger.error(
            "{}|{}|{}".format(stack()[0][3], str(e), request.META.get("REMOTE_ADDR"))
        )
        if settings.DEBUG:
            logger.error(obj.acltext)
    finally:
        if not obj:
            messages.error(request, "Упс, что-то пошло не так, данные не сохранены")
            logger.error(
                "{}|{}|{}".format(
                    stack()[0][3],
                    "Данные в БД не сохранены",
                    request.META.get("REMOTE_ADDR"),
                )
            )
            return HttpResponseRedirect(
                reverse("aclcreate_urls", kwargs=({"acl_id": acl_id}))
            )


class AclOver(BaseView, LoginRequiredMixin, View):
    """Страница формирования ACL файла и других активностей"""

    def get(self, request, acl_id=None):
        if (
            acl_id is None or "LOCAL_STORAGE" not in request.session
        ):  # or 'uuid' not in request.session
            return HttpResponseRedirect(reverse("acldemo_urls"))
        context = {
            "obj": acl_id,
            "JS_TIMEOUT": settings.JS_TIMEOUT,
            "tasks": {},
        }
        if "uuid" in request.session is not None:
            if "/new/" in request.path:
                if str(acl_id) != request.session["uuid"]:
                    return HttpResponseRedirect(reverse(FORM_URLS[0]))
            elif str(acl_id) == request.session["uuid"]:
                return redirect(reverse("acloverview_urls", kwargs={"acl_id": acl_id}))

        if len(request.session["LOCAL_STORAGE"]) > 1:  # or tmp.status == 'NOTFL'
            owner_form = request.session["LOCAL_STORAGE"][FORM_APPLICATION_KEYS[0]]
            obj = save__form(request, owner_form, acl_id)
            if obj is not None:
                return obj
        else:
            messages.warning(request, "Нехватает данных для формирования ACL")
            return redirect(reverse(FORM_URLS[1], kwargs={"acl_id": acl_id}))

        try:
            tmp = get_object_or_404(ACL, id=str(acl_id))
            if len(tmp.taskid) > 4:
                context.update({"taskid": tmp.taskid})
        except tmp.DoesNotExist:
            messages.error(
                request,
                "К сожалению такого ACL нету, вероятно Вы ошиблись, либо истекла сессия",
            )
            return HttpResponseRedirect(reverse("acldemo_urls"))

        """Проверяем состояние массива с данными"""
        # and all(KEY in request.session['LOCAL_STORAGE'] for KEY in FORM_APPLICATION_KEYS):

        if tmp.status == "WTE":
            return HttpResponseRedirect(
                reverse("acl_approve_urls", kwargs={"acl_id": acl_id})
            )

        # Требовать согласование при формировании обращения
        if "ACT_MAKE_GIT" in request.session or "ACT_OMNI" in request.session:

            if tmp.status in ["APRV", "CMP"] and tmp.approve == None:
                messages.error(
                    request,
                    "Данный ACL уже согласован, но нам не удалось получить данные согласующего. "
                    "Необходимо согласовать заново.",
                )
                tmp.status = "FL"
                tmp.save()
                t = reverse("acl_approve_urls", kwargs={"acl_id": acl_id})
                return HttpResponseRedirect(t)

            if (
                tmp.status in ["FL", "CNL"]
                or tmp.status == "CMP"
                and tmp.approve == None
            ):
                t = reverse("acl_approve_urls", kwargs={"acl_id": acl_id})
                return HttpResponseRedirect(t)

            if tmp.status not in ["APRV", "CMP", "JOB"]:
                messages.warning(request, "Данный ACL не получил согласования")
                return HttpResponseRedirect(
                    reverse("acl_approve_urls", kwargs={"acl_id": acl_id})
                )

        if "ACT_MAKE_DOCX" in request.session:
            context["tasks"].update(
                {"list-group-item-file": "Формирование docx обращения"}
            )

        if "ACT_OMNI" in request.session:
            context["tasks"].update(
                {"list-group-item-omni": "Отправка обращения в OmniTracker"}
            )

        if "ACT_MAKE_GIT" in request.session:
            context["tasks"].update({"list-group-item-git": "Отправка md в Git"})
        # tmp_cache = cache.get(acl_id, {})
        # if len(tmp_cache) > 0:
        #     context.update({'cache': tmp_cache})
        context.update({"status": tmp.status})
        return render(request, "acl_overview.html", context=context)


@csrf_exempt
def acl_stage_change(request, *args, **kwargs):
    if request.method != "POST":
        return HttpResponse(status=405)

    logger.info(f"Проверка request.user: {request.user}")

    if not request.user.is_authenticated:
        logger.error("Обнаружен Анонимный пользователь.")
        messages.error(
            request,
            "Вы не авторизованы. Необходимо авторизоваться для изменения статуса ACL. Пожалуйста, авторизуйтесь.",
        )
        result = {
            "error": "Вы не авторизованы. Необходимо авторизоваться для изменения статуса ACL. Пожалуйста, авторизуйтесь."
        }
        send_to_mattermost(
            "[СОГЛАСОВАНИЕ] Обнаружен Анонимный пользователь. Изменение статуса прекращено."
        )
        return HttpResponse(json.dumps(result), content_type="application/json")

    uuid = request.POST.get("uuid", "")
    text = request.POST.get("text", "")
    stage = request.POST.get("stage", "")

    if not uuid or not stage:
        result = {"error": "Неверная request data"}
        return HttpResponse(json.dumps(result), content_type="application/json")

    valid_stages = ["NOTFL", "FL", "CMP", "WTE", "APRV", "CNL", "JOB"]
    if stage not in valid_stages:
        result = {"error": "Неверный stage"}
        return HttpResponse(json.dumps(result), content_type="application/json")

    if stage == "WTE":
        result = {"error": "Не удалось изменить статус на WTE"}
        return HttpResponse(json.dumps(result), content_type="application/json")

    try:
        acl = ACL.objects.get(id=uuid)
    except ACL.DoesNotExist:
        result = {"error": "ACL не найден"}
        return HttpResponse(json.dumps(result), content_type="application/json")

    if (
        stage in ["APRV", "CNL"]
        and request.user not in acl.approve.all()
        and "token" not in request.META.get("HTTP_REFERER")
    ):
        logger.error(
            f"Пользователю {request.user} не хватает прав для изменения статуса ACL."
        )
        messages.error(request, "У Вас не достаточно прав для изменения статуса ACL.")
        result = {"error": "У Вас не достаточно прав для изменения статуса ACL."}
        return HttpResponse(json.dumps(result), content_type="application/json")

    if text == "" and stage == "CNL":
        text = "Отклонено согласующим без указании причины"

    acl.approve.set([request.user])
    acl.status = stage
    acl.comment = text[:127]
    acl.token = MakeTemporaryToken()
    acl.save()

    log_message = f"[ACL PORTAL] Статус ACL({uuid}) изменён на новый:{stage}."
    print(log_message)
    logger.info(log_message)

    if stage == "APRV" and settings.MAKE_TASK_AFTER_APRROVE:
        sendtext = "Ваше обращение согласовано и уже отправлено на исполнение"
        logger.info(f"Обращение {uuid} будет выполнено через бекенд")
        if settings.DEBUG:
            print(f"Обращение {uuid} будет выполнено через бекенд")

        threading.Thread(
            target=MakeBackgroundTask, kwargs={"request": request, "acl_id": str(uuid)}
        ).start()

        letter_context = {"sendtext": sendtext, "uuid": uuid}
        email_body = render_to_string(
            "status_change_letter.html", context=letter_context
        )
        try:
            msg = EmailMessage(
                subject="Статус обращения",
                body=email_body,
                from_email="acl@alfastrah.ru",
                to=[acl.owner.email],
            )
            msg.content_subtype = "html"
            msg.send(fail_silently=settings.DEBUG)
        except Exception as e:
            logger.error(f"Ошибка отправки письма 'Статус обращения': {e}")

    result = {"status": f"Статус изменён на новый: {stage}"}
    return HttpResponse(json.dumps(result), content_type="application/json")


@csrf_exempt
def Gitcheck(request):
    """Функция сохранения и проверки git проекта"""
    if request.method == "POST":
        result = {"status": "Git проект и файл корректно загружены"}
        if "git_url" in request.POST:
            if request.POST.get("git_url", "") == "":
                if "ACT_MAKE_GIT" in request.session:
                    del request.session["ACT_MAKE_GIT"]
                result = {"error": "Git проект не может быть пустым"}
        return HttpResponse(json.dumps(result), content_type="application/json")
    return HttpResponse(status=405)


def get_gitlab_files(request):
    if request.method == "POST":
        try:
            gitlab_project = request.POST.get("gitlab_project")
            gitlab_repo_url = ACLGitlabStore.objects.get(
                project=gitlab_project
            ).gitlab_url
            md_files = get_files_from_gitlab(
                repo_url=gitlab_repo_url, branch_name="develop"
            )
            return HttpResponse(json.dumps(md_files), content_type="application/text")
        except Exception as e:
            logger.error(f"Ошибка при загрузке: {e}")
    return HttpResponse(json.dumps([]), content_type="application/text")


def upload_acl_from_git(request):
    if request.method == "POST":
        logger.info("[Загрузка md на портал] Инициализация загрузки md на портал")
        gitlab_project = request.POST.get("project")
        gitlab_repo_url = ACLGitlabStore.objects.get(project=gitlab_project).gitlab_url
        gitlab_file_name = request.POST.get("gitlab_file_selected_option")
        logger.info(
            f"[Загрузка md на портал] Получены данные для загрузки: gitlab_project:{gitlab_project}, gitlab_repo_url:{gitlab_repo_url}, gitlab_file_name:{gitlab_file_name}"
        )
        # Проверка прав пользователя
        session = requests.Session()
        session.headers.update({"PRIVATE-TOKEN": settings.GIT_ACCESS_TOKEN})
        project_id = get_gitlab_project_info(
            session, repo_url=gitlab_repo_url, mode="id"
        )
        logger.info(f"[Загрузка md на портал] Получен id Проекта:{project_id}")
        get_members_api = (
            f"https://gitlab.alfastrah.ru/api/v4/projects/{project_id}/members/all"
        )
        #
        response = session.get(get_members_api)

        if response.status_code == 200:
            project_members_list_full = response.json()
            project_members_list = []
            for member in project_members_list_full:
                project_members_list.append(member["username"].lower())
            current_username = request.session["GIT_USERNAME"]

            if current_username.lower() in project_members_list:
                logger.info(
                    f"[Загрузка md на портал] Пользователь {current_username} есть в списке участников: {project_members_list}. Начинаю загрузку md на портал"
                )
                g = GitWorker(
                    request,
                    gitlab_repo_url,
                    PATH_OF_GIT_REPO=None,
                    MDFILE="",
                    taskid="",
                )
                if g:
                    g.repo.git.ls_remote("--heads", "--tags", g.GITPRO)
                    g.free()
            else:
                logger.warning(
                    f"[Загрузка md на портал] Пользователь {current_username} не найден среди участников проекта: {project_members_list}. Отображено сообщение: нет доступа"
                )
                return HttpResponse(
                    json.dumps(
                        {
                            "error": f"Ошибка: У пользователя {current_username} нет доступа"
                        }
                    ),
                    content_type="application/json",
                )
        else:
            logger.error(
                f"[Загрузка md на портал] Ошибка во время проверки прав:{response}"
            )
            return HttpResponse(
                json.dumps({"error": "Ошибка: Во время проверки прав"}),
                content_type="application/json",
            )

        md_content, project_desc = get_acl_from_gitlab(
            repo_url=gitlab_repo_url, branch_name="develop", file_name=gitlab_file_name
        )
        if md_content is not None:
            result = convert_md_to_dict(md_content)
            if "LOCAL_STORAGE" in result:
                request.session["LOCAL_STORAGE"] = result.get("LOCAL_STORAGE")
                if "acl_create_info.html" in result["LOCAL_STORAGE"]:
                    result["LOCAL_STORAGE"]["acl_create_info.html"][
                        "description"
                    ] = project_desc
        if request.method == "POST" and request.is_ajax:
            if len(result["LOCAL_STORAGE"]) == 0:
                data = {"project_desc": project_desc}
                return HttpResponse(json.dumps(data), content_type="application/text")
            return HttpResponse(json.dumps(result), content_type="application/text")
        return HttpResponse(
            json.dumps("upload from git error"), content_type="application/text"
        )


def send_acl_to_git(request, acl_object):
    try:
        file_md = (
            create_markdown_file(
                request,
                json_data=acl_object.acl_data,
                filename=f"acl_{acl_object.id}",
                fileuuid=acl_object.id,
            )
            or None
        )
        if not file_md:
            send_to_mattermost(
                "[acl_pusher] Ошибка: функция создания md отработала, но файл не сформирован."
            )
            raise Exception(
                "[acl_pusher] Ошибка: функция создания md отработала, но файл не сформирован."
            )

        file_md_abs = os.path.join(BASE_DIR, file_md)
        file_md_abs = os.path.normpath(file_md_abs)
        if not os.path.exists(file_md_abs):
            send_to_mattermost(
                "[acl_pusher] Ошибка: Сформированный md файл не существует."
            )

        gitlab_project = acl_object.project
        gitlab_filename = acl_object.git_filename
        gitlab_repo_url = ACLGitlabStore.objects.get(project=gitlab_project).gitlab_url

        g = GitWorker(
            request,
            GITPRO=gitlab_repo_url,
            PATH_OF_GIT_REPO=None,
            MDFILE=file_md_abs,
            taskid=acl_object.id,
        )
        gitpush_successful = False
        if g:
            g.fetch()
            if g.clone():
                g.repo.git.checkout("develop")
                f = g.activity(gitlab_filename)
                if f:
                    if g.addindex(f):
                        if g.push(refspec="develop:develop"):
                            logger.info("[acl_pusher] acl успешно отправлен в git")
                            send_to_mattermost(
                                "[acl_pusher] acl успешно отправлен в git"
                            )
                            gitpush_successful = True
                        else:
                            send_to_mattermost(
                                "[acl_pusher] acl не удалось отправить в git"
                            )
            g.free()
            return gitpush_successful
    except Exception as e:
        print(f"[acl_pusher] Ошибка отправки в git:{e}")
        send_to_mattermost(f"[acl_pusher] Ошибка при отправке в git: {e}")
        logger.error(f"[acl_pusher] Ошибка при отправке в git: {e}")
        return False


def check_taskid_and_status(request, *args, **kwargs):
    """Проверяет acl в omnitracker, если нет - переотправляет"""

    if (
        request.method == "POST"
        and request.user.is_authenticated
        and request.user.is_superuser
    ):
        acl_list = request.POST.getlist("acl_list[]")
        acl_objects = ACL.objects.filter(id__in=acl_list)

        success_checks_count = 0
        success_send_omni_count = 0
        unsuccessful_count = 0
        unsuccessful_make_docx_count = 0
        unsuccessful_send_git_count = 0

        unsuccessful_make_docx_acl = []
        unsuccessful_git_push_acl = []

        for acl_object in acl_objects:
            try:
                acl_checking_result = acl_sending_retry_checking(acl_object)
                success_checks_count += 1

                if acl_checking_result is None:
                    try:
                        if acl_object.taskid:
                            send_to_mattermost(
                                f"[acl_pusher] [Перехвачен Дубль обращения] ACL уже назначен Номер SD:{acl_object.taskid}; Ссылка на ACL :https://acl.vesta.ru/acl/info/{str(acl_object.id)}"
                            )
                            continue

                        try:
                            gitlab_repo_url = ACLGitlabStore.objects.get(
                                project=acl_object.project
                            ).gitlab_url
                            local_storage = acl_object.acl_data
                            doc_result = make_doc(
                                request,
                                data_set=local_storage,
                                fileuuid=acl_object.id,
                                gitlab_repo_url=gitlab_repo_url,
                                gitlab_filename=acl_object.git_filename,
                            )
                            if settings.OMNITRACKER_URL:
                                try:
                                    docx_url = f"{request.get_host()}/{doc_result[1:]}"
                                    logger.debug("URL ОТПРАВКИ В OMNI " + str(docx_url))

                                    if "://" not in docx_url:
                                        docx_url = "https://" + docx_url

                                except Exception as e:
                                    docx_url = ""
                                    logger.error(
                                        f"Ошибка при создании ссылки на docx: {e}"
                                    )
                                result_id = send_onmitracker(
                                    sender=acl_object.owner.email,
                                    title=f"SH0458 Запрос на предоставление доступа согласован : {str(acl_object.approve.first().get_full_name())}",
                                    text=f"Прошу предоставить сетевой доступ, согласно ACL. Согласование владельца ресурса во вложении. Ссылка на ACL :https://acl.vesta.ru/acl/info/{str(acl_object.id)}",
                                    attach=docx_url,
                                    fake=False,
                                    request=request,
                                    uid=acl_object.id,
                                )
                                result_id = int(result_id) or 0
                                if result_id == 0:
                                    send_to_mattermost(
                                        "[acl_pusher] Мы не смогли создать обращение через OmniTracker"
                                    )
                                    raise Exception(
                                        "[acl_pusher] Мы не смогли создать обращение через OmniTracker"
                                    )
                                else:
                                    if acl_object:
                                        acl_object.taskid = str(result_id)
                                        acl_object.save(update_fields=["taskid"])
                                        success_send_omni_count += 1
                                        send_to_mattermost(
                                            f'[acl_pusher][owner={acl_object.owner}, Ссылка на ACL: https://acl.vesta.ru/acl/info/{str(acl_object.id)}] Получен номер SD("{result_id}"). Добавление и проверка записи на портале={acl_object.taskid}'
                                        )
                            if "ACT_MAKE_GIT" in request.session:
                                del request.session["ACT_MAKE_GIT"]
                            if "ACT_OMNI" in request.session:
                                del request.session["ACT_OMNI"]
                        except Exception as e:
                            send_to_mattermost(
                                f"[acl_pusher] Произошла ошибка при формировании заявки: {e}"
                            )
                            logger.error(
                                f"[acl_pusher] Произошла ошибка при формировании заявки: {e}"
                            )
                            unsuccessful_count += 1

                        try:
                            gitpush_successful = send_acl_to_git(request, acl_object)
                            if not gitpush_successful:
                                unsuccessful_git_push_acl.append(
                                    f"https://acl.vesta.ru/acl/info/{str(acl_object.id)}"
                                )
                                unsuccessful_send_git_count += 1
                        except Exception as e:
                            send_to_mattermost(
                                f"[acl_pusher] Ошибка при создании docx:{e}; Ссылка на ACL :https://acl.vesta.ru/acl/info/{str(acl_object.id)}"
                            )
                            logger.error(f"[acl_pusher] Ошибка при создании docx: {e}")
                            unsuccessful_make_docx_acl.append(
                                f"https://acl.vesta.ru/acl/info/{str(acl_object.id)}"
                            )
                            unsuccessful_count += 1
                            unsuccessful_make_docx_count += 1
                    except Exception as e:
                        send_to_mattermost(
                            f"[acl_pusher] Ошибка при отправке обращения в SD:{e}; "
                        )
                        logger.error(
                            f"[acl_pusher] Ошибка при отправке обращения в SD: {e}"
                        )
                        unsuccessful_count += 1
            except Exception as e:
                send_to_mattermost(
                    f"[acl_pusher] Ошибка при работе с acl({acl_object.id}): {e}; "
                )
                logger.error(
                    f"[acl_pusher] Ошибка при работе с acl({acl_object.id}): {e} "
                )
                unsuccessful_count += 1
                continue
        send_to_mattermost(
            f"[acl_pusher] Результат работы Доталкивателя ACL: ( ACL Проверено: {success_checks_count}; ACL переотправлено: {success_send_omni_count})."
        )

        unsuccessful_count_messages = [
            (
                f"[acl_pusher] Не удалось создать docx: {unsuccessful_make_docx_count}."
                if unsuccessful_make_docx_count > 0
                else ""
            ),
            (
                f"[acl_pusher] Не удалось отправить omni: {unsuccessful_count}."
                if unsuccessful_count > 0
                else ""
            ),
            (
                f"[acl_pusher] Не удалось отправить в git: {unsuccessful_send_git_count}."
                if unsuccessful_send_git_count > 0
                else ""
            ),
            (
                f'[acl_pusher] Не удалось сформировать docx для следующих ACL:{"; ".join(unsuccessful_make_docx_acl)}'
                if len(unsuccessful_make_docx_acl) > 0
                else ""
            ),
            (
                f'[acl_pusher] Не удалось отправить в git следующие ACL:{"; ".join(unsuccessful_git_push_acl)}'
                if len(unsuccessful_git_push_acl) > 0
                else ""
            ),
        ]

        for message in [m for m in unsuccessful_count_messages if m]:
            send_to_mattermost(message)

        return HttpResponseRedirect(reverse("aclhistory_urls"))
    return HttpResponse(json.dumps("acl_pusher error"), content_type="application/text")


def UploadTemplate(request):
    try:
        result = upload_file_handler(request, ParseDocx)
    except Exception as e:
        if request.is_ajax:
            logger.error(
                f"Ошибка загрузки из файла. Формат не определён. Ошибка: {str(e)}"
            )
            result = {"error": "Ошибка загрузки из файла. Формат не определён."}
        else:
            messages.error(request, str(e))
        logger.error(f"{stack()[0][3]} {str(e)}")

    if request.method == "POST" and request.is_ajax:
        return HttpResponse(json.dumps(result), content_type="application/text")
    return HttpResponse(
        json.dumps("upload template error"), content_type="application/text"
    )


def CheckIp(request, ip=None):
    """Функция возвращает данные по IP"""
    return HttpResponse(json.dumps(ip_status(ip)), content_type="application/json")


@csrf_exempt
def AclRemove(request, *args, **kwargs):
    """Функция удалеяет данные по uuid"""
    if request.method == "POST":
        result = {"error": "Ошибка при удалении acl"}
        if "uuid" in request.POST:
            if is_valid_uuid(request.POST.get("uuid", 0)):
                try:
                    obj = ACL.objects.get(id=request.POST.get("uuid"))
                    if obj:
                        if obj.status in ["WTE", "CMP"]:
                            return HttpResponse(
                                json.dumps(
                                    {
                                        "error": "Этот ACL нельзя удалить. Он либо на согласовании либо уже выполнен."
                                    }
                                ),
                                content_type="application/json",
                            )
                        obj.delete()
                        result = {"status": "Запись acl удалена"}
                except ACL.DoesNotExist:
                    result = {"error": "Не всё записи удалены"}
        return HttpResponse(json.dumps(result), content_type="application/json")
    return HttpResponse(status=405)


@csrf_exempt
def task(request, acl_id) -> bool:
    return
    """Функция обработки запросов на выполнение активностей для выполнения обращения"""
    logger.info(
        f"[Отправка в omni] Начинается выполнение task для (request,acl_id):({request},{acl_id})"
    )
    logger.info(
        f"[Отправка в omni] Проверка на валидность uuid:{is_valid_uuid(acl_id)}"
    )
    if not is_valid_uuid(acl_id):
        result = {"error": "Произошла ошибка, uuid не прошёл валидацию."}
        logger.info(
            f'[Отправка в omni] acl_id не прошел валидацию. Выход из функции. Вероятно данный ACL застрял в статусе "Согласовано". |acl_id:{acl_id}.'
        )
        send_to_mattermost(
            f'[Отправка в omni] acl_id не прошел валидацию. Выход из функции. Вероятно данный ACL застрял в статусе "Согласовано". |acl_id:{acl_id}.'
        )
        return HttpResponse(
            json.dumps(result, ensure_ascii=False), content_type="application/json"
        )

    JOB = cache.get(acl_id, {})
    is_work_omni = False
    try:
        obj = ACL.objects.get(id=acl_id)
        logger.info(f"[Отправка в omni] Получен obj по acl_id({acl_id})")
        logger.info(
            f"[Отправка в omni] Проверка данных этого acl: owner({obj.owner}), project:({obj.project}), git_filename:({obj.git_filename})"
        )
        local_storage = json.loads(obj.acltext)
        if local_storage == "":
            result = {"error": "Произошла ошибка: не найдены данные acl."}
            log_message = f'[Отправка в omni] local_storage в БД пустой. Выход из функции. Вероятно данный ACL застрял в статусе "Согласовано". |acl_id:{acl_id}|.'
            logger.info(log_message, f" local_storage:{local_storage}.")
            send_to_mattermost(log_message)
            return HttpResponse(
                json.dumps(result, ensure_ascii=False), content_type="application/json"
            )
    except obj.DoesNotExist:
        log_message = f'[Отправка в omni] Не удалось найти объект с acl_id:{acl_id}. Выход из функции. Вероятно данный ACL застрял на статусе "Согласовано".'
        logger.info(log_message)
        send_to_mattermost(log_message)
        return HttpResponse(
            json.dumps("Нет такого ACL", ensure_ascii=False),
            content_type="application/json",
        )

    except Exception as e:
        log_message = f'[Отправка в omni] Произошла ошибка:{e}. Выход из функции. Вероятно данный ACL({acl_id}) застрял на статусе "Согласовано".'
        logger.info(log_message)
        send_to_mattermost(log_message)
        return HttpResponse(
            json.dumps("Нет такого ACL", ensure_ascii=False),
            content_type="application/json",
        )

    if obj.status == "FLY":
        logger.info(
            '[Отправка в omni] Статус == FLY(В процессе). Выход из функции. Вероятно данный ACL застрял на статусе "Согласовано".'
        )
        send_to_mattermost(
            '[Отправка в omni] Статус == FLY(В процессе). Выход из функции. Вероятно данный ACL застрял на статусе "Согласовано".'
        )
        return HttpResponse(
            json.dumps({"status": JOB}, ensure_ascii=False),
            content_type="application/json",
        )

    if any([job for job in tasklist if job in request.session]) and JOB.__len__() == 0:
        cache.set(acl_id, {})
        if obj.taskid != "":
            logger.info("Номер существует")
        else:
            logger.info("[НОМЕР обращения не задан!]")
            obj.status = "FLY"
            with transaction.atomic():
                obj.save(update_fields=["status"])

    sleep(1)
    is_work_done = False
    result_id = None
    doc_ready = False
    if "ACT_MAKE_GIT" in request.session:
        update_callback_status(
            request, acl_id, "git_upload_status", "Генерация md файла"
        )

    if "ACT_MAKE_DOCX" in request.session:
        update_callback_status(
            request, acl_id, "docx_download_status", "Генерация docx файла"
        )
    try:
        gitlab_filename = obj.git_filename
        gitlab_project = obj.project
        gitlab_repo_url = ACLGitlabStore.objects.get(project=gitlab_project).gitlab_url
        logger.info(
            f"[Отправка в omni] Проверка данных полученных перед формированием файла: filename:{gitlab_filename}, project:{gitlab_project}, repo_url:{gitlab_repo_url}",
        )
        logger.info(
            f"[Отправка в omni] Проверка local_storage перед формированием word:{local_storage}"
        )
        doc_result = make_doc(
            request,
            local_storage,
            gitlab_repo_url=gitlab_repo_url,
            gitlab_filename=gitlab_filename,
        )
        logger.info(f"[Отправка в omni] Сформирован word документ: {doc_result}")
        doc_ready = True
    except Exception as e:
        logger.error(
            f"[Отправка в omni] Произошла ошибка при создании docx файла: {e}. |acl_id:{acl_id}|local_storage:{json.loads(obj.acltext)}|"
        )
        send_to_mattermost(
            f"[Отправка в omni]. |acl_id:{acl_id}| Произошла ошибка при создании docx файла: {e}"
        )
        update_callback_status(
            request,
            acl_id,
            "docx_download_status",
            f"Произошла ошибка при создании docx файла: {e}",
            0,
        )
        is_work_done = False
    finally:
        logger.info(
            f"[Отправка в omni] Выполнение finally в task. doc_ready:{doc_ready}"
        )
        if doc_ready:
            if "ACT_MAKE_DOCX" in request.session:
                del request.session["ACT_MAKE_DOCX"]
                logger.info(
                    "Файл docx успешно сформирован. ACT_MAKE_DOCX удалён из сессии."
                )
                update_callback_status(
                    request,
                    acl_id,
                    "docx_download_status",
                    "Файл docx успешно сформирован",
                    2,
                )
    if "ACT_OMNI" in request.session:
        logger.error("ПРЕВЕД МЕДВЕД")
        try:
            if obj.taskid != "":
                send_to_mattermost(
                    f"[Перехвачен Дубль обращения] ACL уже назначен Номер SD:{obj.taskid}; Ссылка на ACL :https://acl.vesta.ru/acl/info/{str(obj.id)}"
                )
                return HttpResponse(
                    json.dumps(
                        {"status": obj.status, "isdouble": True}, ensure_ascii=False
                    ),
                    content_type="application/json",
                )
            is_work_omni = True
            omni_job = cache.get(acl_id, {})
            if settings.DEBUG:
                logger.debug(f"OMNI CACHE STATUS: {omni_job}")

            if omni_job.keys().__len__() > 0 or (
                obj.status == "JOB" and len(obj.taskid) >= 3
            ):
                if "omni_email_status" in omni_job:  # or obj.taskid == 'PRO':
                    if settings.DEBUG:
                        logger.debug(
                            "[TRACE] Активность {} пропущена ввиду незавершенного предыдушей задачи: {} {}".format(
                                omni_job, obj.taskid
                            )
                        )
                    return HttpResponse(
                        json.dumps({"status": JOB}, ensure_ascii=False),
                        content_type="application/json",
                    )
            if doc_ready:
                update_callback_status(
                    request,
                    acl_id,
                    "omni_email_status",
                    "Отправляем запрос на сервер ... (Пожалуйста, подождите)",
                )
            else:
                try:
                    update_callback_status(
                        request, acl_id, "omni_email_status", "Генерация docx файла..."
                    )
                    gitlab_filename = obj.git_filename
                    gitlab_project = obj.project
                    gitlab_repo_url = ACLGitlabStore.objects.get(
                        project=gitlab_project
                    ).gitlab_url
                    logger.info(
                        f"[send omni 2] Проверка local_storage из БД: {local_storage}"
                    )
                    doc_result = make_doc(
                        request,
                        local_storage,
                        acl_id,
                        gitlab_repo_url=gitlab_repo_url,
                        gitlab_filename=gitlab_filename,
                    )
                    doc_ready = True
                except Exception as e:
                    logger.error(e)
                    send_to_mattermost(
                        f"[send omni 2] |acl_id:{acl_id}| Ошибка повторного формирования word:{e}"
                    )

            try:
                if settings.OMNITRACKER_URL:
                    try:
                        docx_url = f"{request.get_host()}/{doc_result[1:]}"
                        logger.debug("URL ОТПРАВКИ В OMNI " + str(docx_url))

                        if "://" not in docx_url:
                            docx_url = "https://" + docx_url

                    except:
                        docx_url = ""
                    result_id = send_onmitracker(
                        sender=obj.owner.email,
                        title=f"SH0458 Запрос на предоставление доступа согласован : {str(ACL.objects.get(id=acl_id).approve.first().get_full_name())}",
                        text=f"Прошу предоставить сетевой доступ, согласно ACL. Согласование владельца ресурса во вложении. Ссылка на ACL :https://acl.vesta.ru/acl/info/{str(acl_id)}",
                        attach=docx_url,
                        fake=False,
                        request=request,
                        uid=acl_id,
                    )
                result_id = int(result_id) or 0
                if result_id == 0:
                    # UpdateCallBackStatus(request, uid, 'omni_email_status',
                    # 'Мы не смогли создать обращение через OmniTracker, отправим почтой...')
                    raise Exception(
                        "Мы не смогли создать обращение через OmniTracker, отправляем почтой..."
                    )
                else:

                    if obj:
                        obj.taskid = str(result_id)
                        #     obj.status = 'JOB'
                        obj.save(update_fields=["taskid"])
                        send_to_mattermost(
                            f'[owner={obj.owner}, Ссылка на ACL: https://acl.vesta.ru/acl/info/{str(obj.id)}] Получен номер SD("{result_id}"). Добавление Номера SD в БД. Проверка записи: obj.taskid={obj.taskid}'
                        )
                    update_callback_status(
                        request,
                        acl_id,
                        "omni_email_status",
                        f'<p class="text-success">Обращение под номером {result_id} успешно зарегистрировано</p>',
                        2,
                    )
                    update_callback_status(
                        request, acl_id, "omni_track_id", f"{result_id}"
                    )
                    if "ACT_OMNI" in request.session:
                        del request.session["ACT_OMNI"]

            except Exception as e:
                logger.error(
                    "{}|{}|{}".format(
                        stack()[0][3], str(e), request.META.get("REMOTE_ADDR")
                    )
                )
                update_callback_status(
                    request, acl_id, "omni_email_status", f"{str(e)}"
                )
                sleep(3)  # Ждем callback для уведомления пользователю

                doc_result = os.path.join(BASE_DIR, doc_result[1:])
                logger.error(
                    "ТЕМА ПИСЬМА "
                    + "SH0458 Предоставление доступа  Согласовано = "
                    + str(ACL.objects.get(id=acl_id).approve.first().get_full_name())
                )

                e = EmailMessage(
                    subject="SH0458 Запрос на предоставление доступа согласован :  "
                    + str(ACL.objects.get(id=acl_id).approve.first().get_full_name()),
                    body="Прошу предоставить сетевой доступ, согласно ACL. Согласование владельца ресурса во вложении. Ссылка на ACL : https://acl.vesta.ru"
                    + "/acl/info/"
                    + str(acl_id),
                    from_email=obj.owner.email,
                    to=[settings.EMAIL_SD],
                    reply_to=[settings.EMAIL_ADMIN],
                )
                e.attach_file(doc_result)
                e.send(fail_silently=settings.DEBUG)
                is_work_done = True
                # obj.taskid = str(result_id)
                # obj.status = 'JOB'
                # obj.save(update_fields=['taskid', 'status'])

        except Exception as e:
            update_callback_status(
                request,
                acl_id,
                "omni_email_status",
                "Произошла ошибка при отправки обращения в SD",
                0,
            )
            logger.error(str(e))
            is_work_done = False
        finally:
            if doc_result:
                if "ACT_OMNI" in request.session:
                    del request.session["ACT_OMNI"]
                if result_id is not None and result_id > 0:
                    update_callback_status(
                        request,
                        acl_id,
                        "omni_email_status",
                        f'<p class="text-success">Обращение под номером {result_id} успешно зарегистрировано</p>',
                        2,
                    )
                else:
                    update_callback_status(
                        request,
                        acl_id,
                        "omni_email_status",
                        "Обращение успешно создано (через SD, номер в почте).",
                        2,
                    )

    if "ACT_MAKE_GIT" in request.session:
        # UpdateCallBackStatus(request, acl_id, 'git_upload_status', 'Генерация md файла')
        try:
            file_md = (
                create_markdown_file(request, local_storage, f"acl_{acl_id}", acl_id)
                or "None"
            )
            if not file_md:
                send_to_mattermost(f"Не удалось создать md файл. acl_id:{acl_id}")
                raise Exception("Ошибка при создании md файла")

            update_callback_status(
                request,
                acl_id,
                "git_upload_file",
                f"<a href='{file_md}'style='font-size: 16px' "
                "class='card-link card-download-file text-primary' "
                "target='_blank' download>"
                "<i class='fab fa-github pr-1'>"
                "</i> Скачать md-файл</a>",
                2,
            )

            file_md_abs = os.path.join(
                BASE_DIR, "static/md/" + f"acl_{str(acl_id)}" + ".md"
            )
            if "/" in file_md_abs:
                if "linux" not in sys.platform:
                    file_md_abs = file_md_abs.replace("/", "\\")
            if not os.path.exists(file_md_abs):
                file_md_abs = os.path.join(
                    BASE_DIR, "static/md/" + f"acl_{str(acl_id)}" + ".md"
                )
                logger.error("[Формирование md] Ошибка при формировании пути md файла.")
                update_callback_status(
                    request,
                    acl_id,
                    "git_upload_status",
                    "Ошибка при формировании пути md файла",
                    0,
                )
                # return HttpResponse(json.dumps({'status': cache.get(acl_id, {})}), content_type="application/json")

            update_callback_status(
                request, acl_id, "git_upload_status", "Отправка запроса в gitlab"
            )
            gitlab_project = obj.project
            gitlab_filename = obj.git_filename
            gitlab_repo_url = ACLGitlabStore.objects.get(
                project=gitlab_project
            ).gitlab_url

            g = GitWorker(
                request,
                gitlab_repo_url,
                PATH_OF_GIT_REPO=None,
                MDFILE=file_md_abs,
                taskid=acl_id,
            )
            if g:
                g.pull()
                if g.clone():
                    g.repo.git.checkout("develop")
                    f = g.activity(gitlab_filename)
                    if f:
                        if g.addindex(f):
                            update_callback_status(
                                request,
                                acl_id,
                                "git_upload_status",
                                "Отправка изменений на сервер",
                            )
                            if g.push(refspec="develop:develop"):
                                update_callback_status(
                                    request,
                                    acl_id,
                                    "git_upload_status",
                                    f"Файл {gitlab_filename} успешно загружен в репозиторий",
                                    2,
                                )
                                is_work_done = True
                                send_to_mattermost(
                                    f"[Отправка изменений в giltab] acl({acl_id}) успешно отправлен в gitlab"
                                )
                                if settings.DEBUG:
                                    logger.debug("Файл загружен в проект")
                            else:
                                send_to_mattermost(
                                    f"[Отправка изменений в giltab] Не удалось отправить acl({acl_id}) в gitlab"
                                )
                g.free()
        except Exception as e:
            logger.error(f"Ошибка при отправке в git: {e}")
            update_callback_status(request, acl_id, "git_upload_status", f"{e}", 0)
            # is_work_done = False
        finally:
            if "ACT_MAKE_GIT" in request.session:
                del request.session["ACT_MAKE_GIT"]
            if settings.DEBUG:
                logger.debug("Очистка переменных GIT")

        # obj = ACL.objects.get(id=request.POST.get('uuid'))

    JOB = cache.get(acl_id, {})

    if obj and (
        (obj.status == "APRV" and obj.approve) or (obj.status == "FL" and is_work_done)
    ):
        if is_work_omni:
            obj.status = "JOB"
        else:
            obj.status = (
                "APRV"  # Фикс баги если сформировать docx то статус будет на исполнении
            )
    if result_id:
        obj.taskid = str(result_id)
    # else:
    # obj.status = 'FL'
    if is_work_done or obj.status == "FLY":
        if is_work_omni:
            obj.status = "JOB"
        else:
            obj.status = (
                "APRV"  # Фикс баги если сформировать docx то статус будет на исполнении
            )
        with transaction.atomic():
            obj.save(update_fields=["status", "taskid"])
    if settings.DEBUG:
        print(
            f"REQUEST FINISH, SAVING DATA: {[job for job in tasklist if job in request.session]}: {JOB}"
        )

    return HttpResponse(
        json.dumps({"status": "complete"}, ensure_ascii=False),
        content_type="application/json",
    )


@csrf_exempt
def act(request, acl_id=None, job=None, do=None):
    result = {"done": "Данные сохранены"}
    obj = None
    if job not in jobs or do not in ["remove", "add"]:
        return HttpResponseForbidden(request)

    try:
        obj = ACL.objects.get(id=acl_id)
    except:
        return HttpResponseNotFound(request)

    old = str(obj.activity).split(";")
    if do == "remove":
        for j in jobs:
            if j == job:
                if tasklist[jobs.index(j)] in old:
                    old.remove(tasklist[jobs.index(j)])
                    # if j == 'git':
                    #         git_url = [x for x in old if ':' in x]
                    #         if len(git_url) > 0: old.remove(git_url[0])
                    # if 'GIT_URL' in request.session: del request.session['GIT_URL']
                    # if 'GIT_FILENAME' in request.session: del request.session['GIT_FILENAME']
                    if tasklist[jobs.index(j)] in request.session:
                        del request.session[tasklist[jobs.index(j)]]
                    break
    else:
        if tasklist[jobs.index(job)] not in old:
            old.append(tasklist[jobs.index(job)])
            request.session[tasklist[jobs.index(job)]] = True

            # if job == 'git' and request.POST.get('git_url', '') != '':
            #     request.session['GIT_URL'] = request.POST.get('git_url')
            #     old.append(request.POST.get('git_url'))
            # if job == 'git' and request.POST.get('git_file', '') != '':
            #     request.session['GIT_FILENAME'] = request.POST.get('git_file')
            #     old.append(request.POST.get('git_file'))

    # if len(old) > 0:
    old = ";".join(old)
    if old != obj.activity:
        obj.activity = old
        obj.save(update_fields=["activity"])
    return HttpResponse(
        json.dumps(result, ensure_ascii=False), content_type="application/json"
    )


def taskstatus(request, taskid=None):
    """Функция возвращает данные по taskid"""
    omni_acl_status = ""
    omni_http_status = "Статус неизвестен"
    result = {str(taskid): {omni_http_status: omni_acl_status}}
    obj = None

    # obj = ACL.objects.filter(owner=request.user).order_by('-pkid')
    # if taskid == '00000' and request.user:
    #     obj = ACL.objects.filter(owner=request.user).order_by('-pkid')
    #     if len(obj) > 1:
    #         obj = obj[0]
    #     if obj.taskid and re.match(r'\d{3,}', obj.taskid):
    #         taskid = obj.taskid
    #     else:
    #         return HttpResponse(json.dumps(omni_http_status, ensure_ascii=False), content_type="application/json")
    # else:
    #     try:
    #         obj = ACL.objects.filter(id__exact=taskid).order_by('-pkid')
    #     except Exception as e:
    #         logger.error(str(e))
    try:
        if taskid and re.match(r"^\d{3,}$", taskid):
            obj = ACL.objects.filter(taskid__exact=taskid).order_by("-pkid")
        else:
            obj = ACL.objects.filter(id__exact=taskid).order_by("-pkid")
    except Exception as e:
        logger.error(str(e))

    if obj:
        obj = obj[0]  # first el
        if obj.taskid and re.match(r"^\d{3,}$", obj.taskid):
            omni_acl_status, omni_http_status = omni_check_status(obj.taskid)
            if omni_acl_status is None:
                result = {
                    "error": "Could not check omni status, omni_check_status[1422] return None"
                }
            else:
                result = {str(obj.id): {omni_http_status: omni_acl_status}}

        if omni_acl_status != obj.status:
            if omni_acl_status in ["CMP", "JOB", "CNL"]:
                obj.status = omni_acl_status
                obj.save(update_fields=["status"])
    result = {str(taskid): {omni_http_status: omni_acl_status}}
    return HttpResponse(
        json.dumps(result, ensure_ascii=False)
    )  # content_type="application/json",


def TaskStatus(request, acl_id):
    if not is_valid_uuid(acl_id):
        return HttpResponse(
            json.dumps(
                {
                    "error": "Произошла ошибка, вероятно отсутствуют данные для выполнения."
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
    return HttpResponse(
        json.dumps({"status": cache.get(acl_id, {})}, ensure_ascii=False),
        content_type="application/json",
    )


def MakeBackgroundTask(*args, **kwargs):
    request = kwargs.get("request")
    acl_id = kwargs.get("acl_id")
    task(request, acl_id)


def sync_gitlab_projects(request=None):
    try:
        new_projects_list = []
        gitlab_project_list = sync_acl_portal_projects_list()
        for gitlab_project in gitlab_project_list:
            project_obj, created = ACLGitlabStore.objects.get_or_create(
                gitlab_url=gitlab_project["http_url_to_repo"]
            )
            if created:
                project_obj.project = gitlab_project["full_path"]
                project_obj.gitlab_url = gitlab_project["http_url_to_repo"]
                project_obj.save()

                new_projects_list.append(model_to_dict(project_obj))
        return HttpResponse(
            json.dumps(new_projects_list), content_type="application/text"
        )
    except Exception as e:
        logger.error(f"[Ошибка при Обновлении списка проектов] {e}")
        return HttpResponse(json.dumps([]), content_type="application/text")


def get_project_filter_by_department(request=None):

    try:
        team_id = request.GET.get("team_id")
        if team_id != "":
            team_obj = Team.objects.get(id=team_id)
            git_group_url = team_obj.gitlab_group_url
            return HttpResponse(
                json.dumps(git_group_url), content_type="application/text"
            )
        return HttpResponse(json.dumps([]), content_type="application/text")
    except Exception:
        logger.error("Ошибка при получении фильтра")


def set_team_id(request=None):
    try:
        team_name = request.GET.get("team_name")
        if team_name != "" and team_name != "Нет":
            team_obj = Team.objects.get(name=team_name)
            team_id = team_obj.id
            return HttpResponse(json.dumps(team_id), content_type="application/text")
        return HttpResponse(json.dumps([]), content_type="application/text")
    except Exception:
        logger.error("Ошибка при установке teamid")


def makeAndDownloadMdAndDocx(request, acl_id):
    file_path = None
    acl_obj = ACL.objects.get(id=acl_id)
    local_storage = acl_obj.acl_data
    gitlab_filename = local_storage.get("acl_create_info.html", {}).get("file_name", "")

    file_type = request.GET.get("file_type") or None
    try:
        if file_type is not None:
            logger.info(f"[MAKE FILE] Получен file_type:{file_type}")
            if file_type == "docx":
                logger.info("[MAKE FILE] Создание docx файла")
                acl_project = local_storage.get("acl_create_info.html", {}).get(
                    "project", ""
                )
                gitlab_repo_url = ACLGitlabStore.objects.get(
                    project=acl_project
                ).gitlab_url
                file_path = make_doc(
                    request,
                    local_storage,
                    gitlab_repo_url=gitlab_repo_url,
                    gitlab_filename=gitlab_filename,
                )
                logger.info(f"[MAKE FILE] Файл docx успешно сформирован:{file_path}")
            elif file_type == "md":
                logger.info("[MAKE FILE] Создание md файла")
                filename_without_extension = os.path.splitext(gitlab_filename)[0]
                file_path = create_markdown_file(
                    request, local_storage, filename_without_extension, acl_id
                )
                logger.info(f"[MAKE FILE] Файл md успешно сформирован:{file_path}")

            if file_path:
                logger.info(
                    "[MAKE FILE] Путь к файлу получен. Подготовка к скачиванию."
                )
                file_name = os.path.basename(file_path)
                logger.info(f"[MAKE FILE] Получено имя файла:{file_name}")
                logger.info(
                    f"[MAKE FILE] Сформирован абсолютный путь для скачивания:{file_path}"
                )
                file = open(file_path, "rb")
                file_response = FileResponse(file)
                file_response["Content-Disposition"] = (
                    f'attachment; filename="{file_name}"'
                )
                return file_response

        logger.error(
            f"[MAKE FILE] Не удалось сформировать файл. file_type:{file_type}; file_path:{file_path};"
        )
        return redirect(request.META.get("HTTP_REFERER"))
    except Exception as e:
        logger.error(
            f"[MAKE FILE] Exception: Не удалось сформировать файл. file_type:{file_type}; file_path:{file_path}; Ошибка:{e};"
        )
        return redirect(request.META.get("HTTP_REFERER"))


class APIACLInfoView(APIView):
    permission_classes = [HasAPIKey | IsAdminUser]

    class InputSerializer(serializers.Serializer):
        ip_source = serializers.IPAddressField(protocol="IPv4", required=True)
        ip_destination = serializers.IPAddressField(protocol="IPv4", required=True)
        port = serializers.CharField(required=True)

    class OutputSerializer(serializers.Serializer):
        ip_source = serializers.IPAddressField(protocol="IPv4", required=True)
        ip_destination = serializers.IPAddressField(protocol="IPv4", required=True)
        port = serializers.CharField(required=True)
        result = serializers.BooleanField(required=True)

    @swagger_auto_schema(
        operation_description="Получение ACL по IP",
        request_body=InputSerializer,
        responses={
            200: OutputSerializer,
            400: "Bad Request",
        },
        tags=["api/get_acl_by_ip/"],
    )
    def post(self, request):
        data = self.InputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        serializer = data.validated_data

        result = False
        ip_source = serializer.get("ip_source")
        ip_destination = serializer.get("ip_destination")
        port = serializer.get("port").replace(" ", "")

        if ip_source and ip_destination and port:
            queryset = ACL.objects.all().exclude(taskid="")
            for acl in queryset:
                if acl.traffic_rules:
                    for item in acl.traffic_rules:
                        if ip_source == item.get(
                            "sourse_ip"
                        ) and ip_destination == item.get("destination_ip"):
                            row_port = (
                                item.get("protocol_port").split("/")[1].replace(" ", "")
                            )
                            if re.match(r"^\d+$", row_port):
                                if row_port == port:
                                    result = True
                            elif re.match(r"^\d+-\d+$", row_port):
                                start_num, end_num = row_port.split("-")
                                if int(start_num) <= int(port) <= int(end_num):
                                    result = True
                            else:
                                row_port_list = row_port.split(",")
                                for row_port_el in row_port_list:
                                    if re.match(r"^\d+$", row_port_el):
                                        if row_port_el == port:
                                            result = True
                                            break
                                    elif re.match(r"^\d+-\d+$", row_port_el):
                                        start_num, end_num = row_port_el.split("-")
                                        if int(start_num) <= int(port) <= int(end_num):
                                            result = True
                                            break
                    if result:
                        break

        response = self.OutputSerializer(
            {
                "ip_source": ip_source,
                "ip_destination": ip_destination,
                "port": port,
                "result": result,
            }
        ).data
        return Response(data=response, status=status.HTTP_200_OK)


class APITemplatesFilesView(APIView):

    class OutputSerializer(serializers.Serializer):
        id = serializers.CharField()
        name = serializers.CharField()
        path = serializers.CharField()

    @swagger_auto_schema(
        operation_description="Получение списка шаблонов",
        responses={
            200: OutputSerializer,
            400: "Bad Request",
        },
        tags=["api/templates/files/"],
    )
    def get(self, request):
        templates_files = get_templates_files_from_gitlab()
        serializer = self.OutputSerializer(templates_files, many=True)
        return Response(data=serializer.data, status=status.HTTP_200_OK)


class APITemplatesDetailView(APIView):

    class OutputSerializer(serializers.Serializer):
        sourse_ip = serializers.IPAddressField(protocol="IPv4", required=True)
        sourse_domain = serializers.CharField()
        destination_ip = serializers.IPAddressField(protocol="IPv4", required=True)
        destination_domain = serializers.CharField()
        protocol_port = serializers.CharField()
        program_name = serializers.CharField()
        description = serializers.CharField()
        reserve = serializers.BooleanField()

    @swagger_auto_schema(
        operation_description="Получение шаблона",
        responses={
            200: OutputSerializer,
            400: "Bad Request",
        },
        tags=["api/templates/files/"],
    )
    def get(self, request, file_id):
        template = get_templates_from_gitlab(file_id)
        serializer = self.OutputSerializer(template, many=True)
        return Response(data=serializer.data, status=status.HTTP_200_OK)

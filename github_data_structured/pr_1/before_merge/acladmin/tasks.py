import json
import os

from celery import shared_task
from django.apps import apps
from django.db import transaction

from acladmin import settings
from ownerlist.utils import (
    GitWorker,
    celery_send_omnitracker,
    check_acl_in_omni,
    create_markdown_file,
    make_doc,
    send_to_mattermost,
)


@shared_task
@transaction.atomic
def send_acl(acl_id, gitlab_repo_url):
    send_to_mattermost("[acl_portal][Celery send_acl] Cтарт задачи.")
    # 1 Создаём docx файл
    ACL = apps.get_model("accesslist", "ACL")
    acl_object = ACL.objects.select_for_update().get(id=acl_id)

    if acl_object.taskid:
        return

    acl_data_set = json.loads(acl_object.acltext)

    try:
        doc_result = make_doc(
            request=None,
            data_set=acl_data_set,
            fileuuid=acl_object.id,
            gitlab_repo_url=gitlab_repo_url,
            gitlab_filename=acl_object.git_filename,
        )
    except Exception as e:
        print(
            f"[acl_portal][Celery send_acl][DEBUG:{settings.DEBUG}] Не удалось сформировать docx файл:{e}."
        )
        send_to_mattermost(
            f"[acl_portal][Celery send_acl] Не удалось сформировать docx файл:{e}."
        )
        return False
    docx_url = f"https://acl.vesta.ru/{doc_result[22:]}"
    send_to_mattermost(
        f"[acl_portal][Celery send_acl] Старт отправки ACL({acl_object.id}) в omnitracker. Сформированный файл : {docx_url}"
    )
    # 2 Отправляем docx файл в omnitracker
    task_id = celery_send_omnitracker(
        sender=acl_object.owner.email,
        title=f"SH0458 Запрос на предоставление доступа согласован: {str(acl_object.approve.first().get_full_name())}",
        text=f"Прошу предоставить сетевой доступ, согласно ACL. Согласование владельца ресурса во вложении. Ссылка на ACL :https://acl.vesta.ru/acl/info/{str(acl_object.id)}",
        attach=docx_url,
        uuid=acl_object.id,
    )
    send_to_mattermost(
        f"[acl_portal][Celery send_acl] ACL({acl_object.id}) отправлен в omnitracker. Получен task_id:({task_id})"
    )
    if task_id is None:
        print(
            f"[acl_portal][Celery send_acl] Ошибка: Заявку({acl_object.id}) Не удалось отправить в omnitracker."
        )
        send_to_mattermost(
            f"[acl_portal][Celery send_acl] Ошибка: Заявку({acl_object.id}) Не удалось отправить в omnitracker."
        )
        return False

    acl_object.taskid = task_id
    acl_object.status = "JOB"
    acl_object.save()

    # 3 Создаём md файл
    filename_without_extension = os.path.splitext(acl_object.git_filename)[0]
    md_file = create_markdown_file(
        request=None,
        json_data=acl_data_set,
        filename=filename_without_extension,
        fileuuid=acl_object.id,
    )
    if md_file is False:
        print("[acl_portal][Celery send_acl] Ошибка: Не удалось сформировать md файл.")
        send_to_mattermost(
            "[acl_portal][Celery send_acl] Ошибка: Не удалось сформировать md файл."
        )
    else:
        file_md_abs = os.path.join(settings.BASE_DIR, md_file)
        file_md_abs = os.path.normpath(file_md_abs)
        if os.path.exists(file_md_abs):
            # 4 Отправляем md файл в gitlab
            g = GitWorker(
                None,
                gitlab_repo_url,
                PATH_OF_GIT_REPO=None,
                MDFILE=file_md_abs,
                taskid=acl_object.id,
            )
            if g:
                g.pull()
                if g.clone():
                    g.repo.git.checkout("develop")
                    f = g.activity(acl_object.git_filename)
                    if f:
                        if g.addindex(f):
                            if g.push(refspec="develop:develop"):
                                print(
                                    f"[acl_portal][Celery send_acl] acl({acl_object.id}) успешно отправлен в gitlab"
                                )
                            else:
                                print(
                                    f"[acl_portal][Celery send_acl] Не удалось отправить acl({acl_object.id}) в gitlab"
                                )
                                send_to_mattermost(
                                    f"[acl_portal][Celery send_acl] Не удалось отправить acl({acl_object.id}) в gitlab"
                                )
                g.free()
        else:
            print(
                f"[acl_portal][Celery send_acl] Ошибка: Сформированный md файл не существует. Путь:{file_md_abs}"
            )
            send_to_mattermost(
                f"[acl_portal][Celery send_acl] Ошибка: Сформированный md файл не существует. Путь:{file_md_abs}"
            )
    send_to_mattermost("[acl_portal][Celery send_acl] Задача успешно отработала")
    return task_id


@shared_task
def check_acl_status():
    ACL = apps.get_model("accesslist", "ACL")
    acl_objects = ACL.objects.filter(status="JOB", taskid__isnull=False).exclude(
        taskid=""
    )
    for acl_object in acl_objects:
        try:
            check_acl_in_omni(acl_object)
        except Exception as e:
            print(
                f"[acl_portal][Celery check_acl_status] Ошибка проверки статуса acl({acl_object.id}). Ошибка:{e}"
            )
            send_to_mattermost(
                f"[acl_portal][Celery check_acl_status] Ошибка проверки статуса acl({acl_object.id}). Ошибка:{e}"
            )


@shared_task
def test_task():
    print("Test task executed")

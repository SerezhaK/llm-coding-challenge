from django.shortcuts import render
from django.views.generic import View
from django.contrib.auth.mixins import LoginRequiredMixin
from ownerlist.utils import BaseView
from django.conf import *
from ownerlist.utils import BASE_DIR
from django.apps import apps
import os
from .forms import email_form
from django.core.mail import EmailMessage
from django.contrib import messages

def __check_in_buf__(data, value, buff, deep=100, idx=5)->bool:
    """Функция проверки дубликатов в буфере ошибок из лог файла"""
    if len(buff) <= 0:
        return False
    for index, v in enumerate(buff):
        #tmp = value.split('|')
        if len(v) <= 1:
            continue
        if data in v[1] and value == v[idx]:
                if (type(buff[index][-1]) == int):
                    buff[index][-1] += 1
                else:
                    buff[index].append(2)
                return True
        else:
            if (type(buff[index][-1]) != int):
                buff[index].append(1)
        if index >= deep:
            break

    return False

class PanelView(BaseView, LoginRequiredMixin, View):
    """Страница администрирования"""
    def get(self, request):
            context = {
                'debug': settings.DEBUG,
            }
            PS_ACTIVE = False
            MAX_VIEW = 20
            SEVERITY = 0
            try:
                import psutil
                PS_ACTIVE = True
            except ImportError:
                pass
            MAX_VIEW = int(request.GET.get('view', 20))
            SEVERITY = int(request.GET.get('severity', 0))

           #Iplist = apps.get_model('ownerlist', 'Iplist') history = apps.
            history_obj = apps.get_model('ownerlist', 'HistoryCall')
            history = history_obj.objects.order_by('-id')[:MAX_VIEW]
            context.update({'history': history})

            if PS_ACTIVE:
                hdd = psutil.disk_usage('/')
                net = psutil.net_connections(kind='tcp')
                net_cnt = 0
                for el in net:
                    if el.status == 'ESTABLISHED':
                        net_cnt +=1
                context.update({
                    'cpu': psutil.cpu_percent() or 0,
                    'ram': psutil.virtual_memory().percent or 0,
                    'hdd_free': round(hdd.free / (2**30)) or 0,
                    'hdd_total': round(hdd.used / (2 ** 30)) or 0,
                    'net_con': net_cnt,
                })

            buf = []
            idx = 0
            log = os.path.join(BASE_DIR, 'log\\debug.log')
            if os.path.exists(log):
                print(log)
                with open(log, 'r') as f:
                            for line in f:
                                line = line.split('|')
                                if len(line) >= 5:
                                    try:
                                        date = line[1].split(' ')[0]
                                    except IndexError:
                                        continue

                                    if line[0] == 'WARNING':
                                            idx = 6
                                    elif line[0] == 'ERROR':
                                            idx = 5
                                    elif line[0] == 'INFO':
                                        if SEVERITY == 1: continue
                                        idx = 5
                                    elif line[0] == 'DEBUG':
                                        if SEVERITY == 1: continue
                                        idx = 5
                                    else:
                                        continue


                                    if not __check_in_buf__(date,line[idx],buf, 999,idx=idx):
                                        if len(buf) == 0:
                                            line.append(1)
                                        buf.append(line)

            if buf:
                        if len(buf) > MAX_VIEW:
                            buf = buf[-MAX_VIEW:]
                        buf.reverse()


                        context.update({
                            'log': buf,
                        })

            return render(request, 'panel.html', context=context)

    # def post(self, request):
    #     e_form = email_form(data=request.POST or None)
    #     if e_form.is_valid():
    #         e = EmailMessage('Тестовое письмо', e_form.cleaned_data['body'], 'acl@alfastrah.ru',
    #                          to=[e_form.cleaned_data['email']])
    #         e.content_subtype = "html"
    #         if e.send(fail_silently=True):
    #             messages.info(request, 'Письмо отправленно.')
    #         else:
    #             messages.error(request, 'Что-то пошло не так.')
    #     else:
    #         messages.error(request, 'Форма не валидная, нужно проверить данные')
    #     return render(request, 'panel.html')
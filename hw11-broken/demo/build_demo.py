import json, subprocess, textwrap, wave, xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parent
P=ROOT.parent
FONT='C:/Windows/Fonts/arial.ttf'
MONO='C:/Windows/Fonts/consola.ttf'
def font(n,mono=False): return ImageFont.truetype(MONO if mono else FONT,n)
scenes=[]
def scene(title,body,narration,source,plot=None,terminal=False):
    scenes.append(dict(title=title,body=body,narration=narration,source=source,plot=plot,terminal=terminal))
scene('HW11 · Сервис работает, пользователи ждут',
'Kubernetes под нагрузкой\nПрогрев • очередь • обновления • балансировка • HPA\n\nbackend_mode=simulated',
'В этой работе я исследую, почему сервис может работать, а пользователи всё равно ждут. Задача — развернуть API в Kubernetes, воспроизвести эксплуатационные проблемы и подтвердить исправления измерениями. Backend явно обозначен как simulated. Это управляемая симуляция нагрузки, а не реальная языковая модель, GPU-инференс или производственный бенчмарк.',
'docs/homework11.html · report.html')
scene('Архитектура стенда',
'4 клиентских Pod → Service / ClientIP affinity\n                     ↓\nFastAPI · SSE · ограниченная очередь · prefix cache\n                     ↓ /metrics\nPrometheus → Grafana и alert rules\nCPU metrics → HPA: 2–3 реплики\n\nkind: 1 control-plane + 2 worker · Helm Deployment',
'Стенд состоит из локального kind-кластера: один управляющий узел и два рабочих. Helm разворачивает FastAPI, два варианта Service, мониторинг и HPA. Четыре клиентских Pod создают нагрузку с разными исходными IP. Внутри сервиса есть прогрев, ограничение параллелизма, очередь и локальный ограниченный кэш префиксов. Ответ передаётся частями по SSE. Prometheus собирает метрики, Grafana показывает их, а HPA использует CPU.',
'kind-config.yaml · app.py · chart/templates/ · loadgen.py')
scene('Как запустить · команды из README',
'# PowerShell из каталога hw11-broken\n.\\scripts\\setup-kind.ps1\nkubectl get pods,services,hpa -n hw11 -o wide\nkubectl get endpointslices -n hw11\nkubectl top pods -n hw11\nkubectl describe hpa -n hw11\n\n# В отдельном терминале\nkubectl port-forward -n hw11 svc/hw11-hw11-simulator 8080:8080',
'Для повторного запуска нужны Docker с WSL2, kind, kubectl, Helm и Python версии три двенадцать. Из каталога проекта запускается setup-kind. Скрипт собирает образ, загружает его в кластер, устанавливает metrics-server и Helm-чарт. Затем проверяются Pod, Service, EndpointSlice и HPA. Здесь показаны команды для воспроизведения, а результаты опытов далее взяты из сохранённых файлов проекта. Длительные эксперименты в этом видео заново не запускаются.',
'README.md · scripts/setup-kind.ps1',terminal=True)
scene('Readiness и потоковый ответ',
'Invoke-RestMethod http://127.0.0.1:8080/ready\n.\\scripts\\long-stream.ps1\n\n# Сохранённый rollout-stream.log, сокращённо:\nevent: meta\nmodel_version: sim-v2 · backend_mode: simulated\nhistory_is_request_scoped: true\nevent: chunk → ... → event: done',
'После проброса порта маршрут ready показывает готовность backend. Health, startup и ready разделены: живой процесс ещё не обязательно готов принимать нагрузку. Для потокового запроса в проекте есть скрипт long-stream. В сохранённом журнале видны события meta, chunk и done. Метаданные включают Pod, версию и SHA артефакта. История диалога передаётся с каждым запросом. Показанный фрагмент сокращён из реального журнала, а не изображает новый сеанс терминала.',
'app.py · scripts/long-stream.ps1 · results/rollout-stream.log',terminal=True)
scene('Опыт A · прогрев и readiness',
'Ошибочные probes: 114 / 240 завершено · 126 отказов\nИсправленные probes: 233 / 240 завершено · 7 отказов',
'Первый опыт проверяет прогрев. Ошибочные startup и readiness probes обращались к health и включали Pod в обслуживание слишком рано. Из двухсот сорока запросов завершились сто четырнадцать, сто двадцать шесть были отклонены. После переключения на startup и ready завершились двести тридцать три запроса. Семь оставшихся отказов связаны с насыщенной очередью. График показывает сохранённый опыт с ошибочными probes, а сравнение чисел подтверждают два summary-файла.',
'results/bad-probe.summary.json · good-probe.summary.json',plot='01-warmup-ready.svg')
scene('Rolling update и rollback',
'RollingUpdate: maxUnavailable=0 · maxSurge=1\nДлинный SSE-поток пережил обновление\nОтдельная нагрузка: 12 / 12 успешных ответов\n\nНегодный backend не стал Ready\nHelm rollback восстановил sim-v3 и ожидаемый SHA',
'При плановом обновлении используется RollingUpdate без недоступных реплик и с одной дополнительной репликой. Сохранённый длинный поток пережил rollout. Отдельный нагрузочный запуск завершил все двенадцать запросов без обрывов и таймаутов. Затем проверен негодный выпуск: его backend не стал готовым и не попал в Ready endpoints. Откат Helm восстановил рабочую версию sim-v3 и ожидаемый хэш артефакта.',
'chart/templates/deployment.yaml · results/rollout-stream.log · rollout-02.summary.json · rollback-failed-*.json')
scene('Аварийная замена Pod',
'Принудительное удаление → outcome=aborted\nВосстановление нового Pod: ≈ 22,98 с\nВерсия: sim-v3 · тот же ReplicaSet\nПроверены версия, SHA и переданная история\n\nИстория — в запросе; начатый поток не переносится',
'Аварийная замена отличается от планового обновления. После принудительного удаления Pod текущий поток получил статус aborted. Новый Pod восстановился примерно за двадцать две целых девяносто восемь сотых секунды. Проверены версия sim-v3, SHA артефакта и переданная история диалога. Это подтверждает восстановление обработки новых запросов, но не бесшовный перенос уже начатого потока. История принадлежит запросу, а не памяти потерянного Pod.',
'results/replacement-evidence.json · forced-stream.log · tests/test_kubernetes_smoke.py')
scene('Опыт B · контролируемое сравнение',
'.\\scripts\\run-k8s-load.ps1 -RunId demo-bal-n `\n  -Scenario balanced -Policy normal `\n  -DurationSeconds 120 -TotalRps 8\n\n12 запусков: по 3 на balanced/hot × normal/affinity\n120 секунд · 8 RPS · 2 Ready Pod и 2 Ready endpoint\n4 клиентских Pod с разными source IP',
'Для сравнения балансировки выполнено двенадцать контролируемых запусков: по три на каждое сочетание сценария и политики. Каждый запуск длится сто двадцать секунд при восьми запросах в секунду. Timeline подтверждает две готовые реплики и два готовых endpoint. Важны разные исходные IP клиентских Pod: локальные виртуальные клиенты с одним IP не заменяют такой опыт. На экране — пример команды повторного запуска.',
'scripts/run-k8s-load.ps1 · loadgen.py · results/b-*-timeline.jsonl',terminal=True)
scene('Задержки и очередь по Pod',
'Сопоставляем TTFC, очередь и распределение нагрузки\nСредняя CPU может скрывать перегруженную реплику',
'График задержек и очереди разбит по Pod. Он помогает увидеть перекос, который может скрыться за средней загрузкой Deployment. TTFC здесь означает время до первой содержательной части ответа. Это не время до первого токена модели. Клиентские перцентили считаются только по полностью завершённым ответам, поэтому число отказов необходимо показывать рядом. Ограничение очереди делает перегрузку явной, вместо бесконечного ожидания.',
'results/plots/02-pod-latency-queue.svg · results/b-*.csv',plot='02-pod-latency-queue.svg')
scene('Service и ClientIP affinity · результаты',
'Сценарий / политика       Завершено / 2880    Отказы    p95 TTFC\nbalanced / normal              2788              92         1,931 с\nbalanced / affinity            2114             766         1,988 с\nhot / normal                   2880               0         0,948 с\nhot / affinity                 2880               0         1,216 с\n\np95 — среднее трёх run p95, не общий p95 всех запросов',
'В balanced-сценарии обычный Service завершил две тысячи семьсот восемьдесят восемь запросов, affinity — две тысячи сто четырнадцать. Отказов соответственно девяносто два и семьсот шестьдесят шесть. В hot-сценарии обе политики завершили все две тысячи восемьсот восемьдесят запросов. Средний run p95 времени до первой части составил одну целую девятьсот тридцать одну тысячную и одну целую девятьсот восемьдесят восемь тысячных секунды для balanced; ноль целых девятьсот сорок восемь и одну целую двести шестнадцать тысячных для hot.',
'report.html · results/b-*.summary.json')
scene('Что показало сравнение политик',
'Affinity не гарантирует меньшую задержку\nВ balanced заметно выросло число отказов\nВ hot отказов нет, но run p95 выше с affinity',
'На графике показано среднее трёх перцентилей по запускам, а не общий перцентиль объединённой выборки. В этой конфигурации affinity увеличила отказы для balanced и не улучшила задержку для hot. Во всех двенадцати запусках не было aborted и timeout. Вывод относится к конкретному симулятору и его локальному кэшу. Его нельзя превращать в универсальное утверждение о Kubernetes или реальных моделях.',
'results/plots/03-policy-comparison.svg · report.html',plot='03-policy-comparison.svg')
scene('Опыт C · HPA: 2 → 3 → 2',
'CPU target 60% · min=2 · max=3 · stabilization=60 с\nОтдельный опыт: cpuBurnMs=30; затем возвращено 15\n960 запросов: 726 завершено · 234 отклонено',
'Масштабирование проверялось отдельным опытом. Целевая загрузка CPU — шестьдесят процентов, минимум две реплики, максимум три; окно стабилизации уменьшения — шестьдесят секунд. CPU-нагрузку симулятора временно увеличили до тридцати миллисекунд. Timeline подтвердил переход с двух реплик на три и обратно на две. Из девятисот шестидесяти запросов завершились семьсот двадцать шесть, двести тридцать четыре были отклонены. После опыта нагрузку вернули к пятнадцати миллисекундам.',
'chart/templates/hpa.yaml · results/hpa-timeline.jsonl · hpa-scale-down.jsonl',plot='04-hpa-load.svg')
scene('Алерты · наблюдаемая последовательность',
'InferenceFailureRatioHigh: pending → firing\nПосле снижения нагрузки оба правила → inactive\n\nInferenceTTFCHigh уже был firing при первой выборке\nЕго исходное срабатывание не приписываем этому прогону',
'Состояния алертов сохранены из JSON API Prometheus. Правило высокой доли отказов прошло из pending в firing. Позднее оба правила вернулись в inactive. Для корректной интерпретации есть важное ограничение: алерт высокой задержки уже находился в firing при первой выборке. Поэтому нельзя утверждать, что именно этот нагрузочный прогон впервые вызвал его срабатывание. Конфигурация правила и наблюдаемое событие — разные виды доказательств.',
'results/hpa-alerts.jsonl · hpa-alert-clear.json · chart/files/alerts.yml')
scene('Проверки и вывод',
'16 тестов прошли · 3 кластерных smoke пропущены без env\nПрогрев защищает readiness; очередь ограничивает ожидание\nRollout и аварийная потеря Pod — разные сценарии\nAffinity оцениваем по задержкам и отказам на данном стенде\n\nSIMULATED · не LLM · не GPU · не production benchmark',
'Локальная проверка тестов: шестнадцать тестов прошли; три кластерных smoke-теста без специальных переменных окружения пропускаются. Работа показывает, зачем разделять прогрев и готовность, ограничивать очередь, проверять обновления и смотреть метрики по отдельным Pod. Доказательства — исходный код, CSV, журналы и графики. Все числа относятся к одному локальному стенду и симулятору. Это демонстрация эксплуатационных механизмов, а не измерение производительности языковой модели или GPU.',
'tests/ · demo/tests.txt · report.html')

def svg_image(path):
    r=ET.parse(path).getroot(); w=int(r.get('width')); h=int(r.get('height'))
    im=Image.new('RGB',(w*2,h*2),'white'); d=ImageDraw.Draw(im)
    def v(e,k,default=0):
        s=e.get(k,str(default)); return float(s.rstrip('%'))*(w/100 if '%' in s else 1)*2
    for e in r:
        tag=e.tag.split('}')[-1]; fill=e.get('fill','black'); stroke=e.get('stroke','black')
        if tag=='rect': d.rectangle((v(e,'x'),v(e,'y'),v(e,'x')+v(e,'width'),v(e,'y')+v(e,'height')),fill=fill)
        elif tag=='line': d.line((v(e,'x1'),v(e,'y1'),v(e,'x2'),v(e,'y2')),fill=stroke,width=max(1,int(v(e,'stroke-width',1))))
        elif tag in ('polyline','polygon'):
            pts=[tuple(float(x)*2 for x in pair.split(',')) for pair in e.get('points').split()]
            if tag=='polygon': d.polygon(pts,fill=fill)
            else: d.line(pts,fill=stroke,width=max(1,int(v(e,'stroke-width',1))))
        elif tag=='circle':
            x,y,rad=v(e,'cx'),v(e,'cy'),v(e,'r'); d.ellipse((x-rad,y-rad,x+rad,y+rad),fill=fill)
        elif tag=='text':
            t=''.join(e.itertext()); anchor={'middle':'ms','end':'rs'}.get(e.get('text-anchor'),'ls')
            d.text((v(e,'x'),v(e,'y')),t,font=font(int(v(e,'font-size',12))),fill=fill,anchor=anchor)
    return im

def wrap(text,width,f):
    out=[]
    for para in text.split('\n'):
        if f.getlength(para)<=width:
            out.append(para); continue
        line=''
        for word in para.split():
            candidate=(line+' '+word).strip()
            if f.getlength(candidate)>width and line: out.append(line); line=word
            else: line=candidate
        out.append(line)
    return out

if __name__=='__main__':
    (ROOT/'scenes.json').write_text(json.dumps(scenes,ensure_ascii=False,indent=2),encoding='utf-8')
    for i,s in enumerate(scenes):
        im=Image.new('RGB',(1920,1080),'#101e32'); d=ImageDraw.Draw(im)
        d.text((64,28),'HW11  /  ДОКАЗАТЕЛЬСТВА ИЗ ПРОЕКТА',font=font(24),fill='#9fb4ce')
        d.text((1670,28),'SIMULATED',font=font(24),fill='#5de0c5')
        d.text((64,90),s['title'],font=font(48),fill='white')
        if s['plot']:
            plot=svg_image(P/'results/plots'/s['plot']); plot.thumbnail((1760,585))
            im.paste(plot,((1920-plot.width)//2,165))
            y=770; size=30
        else: y=210; size=34 if s['terminal'] else 38
        f=font(30 if i==9 else size,s['terminal'] or i==9)
        for line in wrap(s['body'],1770,f):
            d.text((74,y),line,font=f,fill='#d4e8f8'); y+=size+17
        d.text((65,935),'Источник: '+s['source'],font=font(21),fill='#9fb4ce')
        d.text((65,1005),f'{i+1:02} / {len(scenes):02}     Сохранённые материалы / команды для воспроизведения',font=font(23),fill='#9fb4ce')
        im.save(ROOT/f'slide-{i:02}.png')
    print('Slides:',len(scenes))

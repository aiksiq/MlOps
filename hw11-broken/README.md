# HW11 — сервис под нагрузкой

CPU-only стенд для продолжения API из `hw8910-broken`. В HW11 добавлен отдельный HTTP-сервис, чтобы не менять незакоммиченные файлы предыдущего задания. Backend явно обозначен `backend_mode=simulated`; сгенерированные значения не являются токенами или результатами модели.

## Состав стенда

- FastAPI-сервис с настраиваемым прогревом, ограниченной очередью (`maxWaiting` и `queueTimeoutSeconds`), ограничением конкурентных генераций, потоковым SSE-ответом и ограниченным локальным prefix cache.
- Запросы stateless: `session_id` и история диалога передаются клиентом; ответ содержит request ID, имя Pod, версию и SHA-256 артефакта.
- `startup`, `readiness` и `liveness` имеют отдельные маршруты. Параметр `probes.badProbes=true` намеренно направляет startup/readiness на `/health` для воспроизведения дефекта.
- Helm-чарт разворачивает Deployment, два Service (обычный и `ClientIP` affinity), ConfigMap, HPA 2–3, Prometheus, kube-state-metrics и Grafana.
- Prometheus хранит успешные TTFC и полные задержки в Histogram; логи отдельно учитывают завершённые, отклонённые и оборванные запросы.

## Локальные тесты и smoke

Нужен Python 3.12. Из PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Для локального HTTP smoke, не требующего Docker:

```powershell
$env:WARMUP_SECONDS = "0"
$env:MODEL_VERSION = "local-v1"
.\.venv\Scripts\python.exe -m uvicorn --app-dir . app:app --host 127.0.0.1 --port 8080
```

Во втором PowerShell:

```powershell
.\.venv\Scripts\python.exe .\loadgen.py --url http://127.0.0.1:8080 --duration 10 --total-rps 2 --output .\results\local-smoke.csv
Invoke-RestMethod http://127.0.0.1:8080/metrics
```

HTTP smoke работает на одной локальной машине и **не** заменяет Kubernetes-опыты или разные source IP.
Короткий фактический локальный прогон записан в `results/local-smoke.csv` и `results/local-smoke.summary.json`; он не считается Kubernetes-результатом.

## Docker Compose: приложение и мониторинг

После установки и запуска Docker Desktop:

```powershell
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:8080/ready
Start-Process http://127.0.0.1:3000
```

Grafana доступна локально в режиме Viewer; Prometheus — `http://127.0.0.1:9090`. Удаление только ресурсов этого compose-проекта, включая ограниченный TSDB volume:

```powershell
docker compose down -v
```

## Локальный Kubernetes (kind)

Для запуска локального стенда нужны Docker Engine, включённая аппаратная виртуализация, WSL2/Docker Desktop, `kind`, `kubectl`, Helm и Python 3.12. Установите инструменты при необходимости:

```powershell
winget install --id Docker.DockerDesktop --exact --source winget --accept-source-agreements --accept-package-agreements
winget install --id Kubernetes.kind --exact --source winget --accept-source-agreements --accept-package-agreements
winget install --id Kubernetes.kubectl --exact --source winget --accept-source-agreements --accept-package-agreements
winget install --id Helm.Helm --exact --source winget --accept-source-agreements --accept-package-agreements
```

Запустите Docker Desktop с WSL2 backend, затем в PowerShell из каталога `hw11-broken`:

```powershell
.\scripts\setup-kind.ps1
kubectl get pods,services,hpa -n hw11 -o wide
kubectl top pods -n hw11
kubectl port-forward -n hw11 svc/hw11-hw11-simulator 8080:8080
kubectl port-forward -n hw11 svc/hw11-hw11-simulator-prometheus 9090:9090
kubectl port-forward -n hw11 svc/hw11-hw11-simulator-grafana 3000:3000
```

У `setup-kind.ps1` ограниченный кластер из control-plane и двух workers. Скрипт собирает образ, загружает его в kind, устанавливает metrics-server и ставит Helm chart. HPA использует CPU `requests` приложения; без доступного resource metrics API масштабирование не подтвердится.

## Прогоны

### A — прогрев, rollout и rollback

1. Начните сбор состояния: `.\scripts\capture-k8s-timeline.ps1 -DurationSeconds 240 -Output results\warmup-before.jsonl`.
2. Для исходного дефекта выставьте `probes.badProbes=true` и `simulator.warmupSeconds=20`. У нового Pod startup/readiness будут обращаться к `/health`: Pod попадёт в EndpointSlice до окончания прогрева, а запрос к `/generate` вернёт 503. Сохраните описания Pod, EndpointSlice, логи и идентификаторы отклонённых запросов.
3. Повторите с `probes.badProbes=false`. Исправленная конфигурация проверяет `/startup` и `/ready`; до завершения прогрева рабочий Service не должен выбирать Pod.
4. Для рабочего выпуска A→B заранее задайте `simulator.chunkCount=32`, `simulator.chunkSeconds=0.5`, дождитесь Ready, затем в отдельном PowerShell запустите `.\scripts\long-stream.ps1 -Url http://127.0.0.1:8080`. Пока он выдаёт части, в другом окне начните смену `model.version`. Один ответ длится около 17 s, пересекает начало rollout и укладывается в `terminationGracePeriodSeconds=30`. Параллельно запустите 120-секундный генератор. Сверьте request ID из `results/long-stream.log`, endpoint timeline и полный прогон; после этого верните обычные simulator values.
5. Для заведомо негодной B обновите `model.version` и `model.backendReady=false`; проверьте отсутствие Ready endpoint и выполните `helm rollback hw11 -n hw11`. Запустите интеграционные проверки ниже для версии и SHA после rollback.
6. Отдельно удалите Pod с `--grace-period=0 --force`, сохранив request ID оборванного потока и время восстановления. Этот принудительный отказ не смешивайте с результатами штатного rollout.

Пример обновления проверок и модели:

```powershell
helm upgrade --install hw11 .\chart -n hw11 --create-namespace --set probes.badProbes=true --set simulator.warmupSeconds=20
helm upgrade hw11 .\chart -n hw11 --set probes.badProbes=false
helm upgrade hw11 .\chart -n hw11 --set-string model.version=sim-v2
helm upgrade hw11 .\chart -n hw11 --set-string model.version=bad-v3 --set model.backendReady=false --wait --timeout 45s
helm rollback hw11 -n hw11 --wait --timeout 5m
```

### B — обычный Service против ClientIP affinity

Chart создаёт оба Service над теми же Pod. Скрипт создаёт Indexed Job из четырёх независимых client Pod с разными IP:

Чтобы сравнивать политики при одинаковом числе реплик, перед серией ограничьте HPA двумя репликами. Скрипт нагрузки повторно использует заданные значения для каждого прогона:

```powershell
helm upgrade hw11 .\chart -n hw11 --reuse-values --set autoscaling.minReplicas=2 --set autoscaling.maxReplicas=2 --wait
```

Перед отдельным HPA-опытом верните `autoscaling.maxReplicas=3`.

```powershell
.\scripts\run-k8s-load.ps1 -RunId balanced-01 -Scenario balanced -Policy normal
.\scripts\run-k8s-load.ps1 -RunId balanced-affinity-01 -Scenario balanced -Policy affinity
.\scripts\run-k8s-load.ps1 -RunId hot-01 -Scenario hot -Policy normal
.\scripts\run-k8s-load.ps1 -RunId hot-affinity-01 -Scenario hot -Policy affinity
```

Для каждой комбинации `normal/affinity × balanced/hot` сделайте три прогона по 120 секунд. При `hot` 80% поступлений идёт от client 0, оставшиеся 20% делятся между другими клиентами. Запросы имеют одинаковую детерминированную последовательность prompt/history; у каждой политики новый `run-id`. Скрипт сохраняет CSV по клиентам и объединённый `<run-id>.csv`, включая явные поля `scenario` и `policy`, а также summary с числом наблюдений, успешными p50/p95 TTFC, inter-chunk и полной задержки, очередью, ошибками, тайм-аутами и оборванными потоками. Ключ префикса содержит фиксированный по длине hash `run-id`: каждый прогон начинает с холодного cache, а повторы внутри прогона получают hit; тексты prompt/history и расписание остаются одинаковыми.

Запустите сбор timeline в отдельном PowerShell одновременно с `run-k8s-load.ps1`; после завершения нагрузки сохраните состояние Pod/EndpointSlice:

```powershell
.\scripts\capture-k8s-timeline.ps1 -DurationSeconds 150 -Output results\<run-id>-timeline.jsonl
kubectl get pods -n hw11 -o wide
kubectl get endpointslices -n hw11 -w
kubectl logs -n hw11 -l app.kubernetes.io/instance=hw11 --all-containers --timestamps
```

`ClientIP` определяется IP client Pod; виртуальные клиенты локального `loadgen.py` имеют один source IP и для этого сравнения непригодны. Cache — ограниченная имитация, числа относятся только к simulator workload.

Контролируемая серия уже выполнена при двух Ready-репликах, 8 RPS и 120 s на запуск:

| Сценарий | Политика | Completed / 2,880 | Rejected | Mean run p95 TTFC |
|---|---|---:|---:|---:|
| balanced | normal | 2,788 | 92 (3.2%) | 1.931 s |
| balanced | affinity | 2,114 | 766 (26.6%) | 1.988 s |
| hot | normal | 2,880 | 0 | 0.948 s |
| hot | affinity | 2,880 | 0 | 1.216 s |

Во всех 12 прогонах было 0 aborted и timeout. Эти результаты характеризуют только simulator и его локальный cache. Сводные CSV: `results/b-bal-n-*.csv`, `results/b-bal-a-*.csv`, `results/b-hot-n-*.csv`, `results/b-hot-a-*.csv`; `b-*-timeline.jsonl` подтверждают фиксированное число Ready Pod/endpoints. Job допускает дополнительное время для завершения запросов, поставленных клиентским генератором в ожидание semaphore.

### C — HPA и алерты

По умолчанию HPA настроен на CPU: min 2, max 3, target 60%, scale-down stabilization 60 s. Проверьте `kubectl top pods`/`kubectl describe hpa`, затем повторите нагрузку и дождитесь фактического 2→3→2; сохраните график и HPA conditions. Чтобы повысить вычислительную нагрузку симулятора, временно увеличьте `simulator.cpuBurnMs` и подберите RPS пилотным прогоном. Средняя CPU по репликам может скрыть горячий Pod: новая реплика не обязана получать трафик от уже закреплённых ClientIP.

Два Prometheus alert rules хранятся в `chart/files/alerts.yml`: TTFC >500 ms у более чем 5% успешных ответов и доля rejected/aborted >5%. Для обоих требуется минимум 10 завершённых запросов за 5 минут и удержание 2 минуты. `Pending`/`Firing` и возврат в `Normal` зафиксируйте в Prometheus Alerts. Без достаточного числа наблюдений правило намеренно не срабатывает.

Фактический опыт выполнен с `simulator.cpuBurnMs=30`, min/max HPA 2/3: реплики изменились 2→3→2, затем нагрузка была возвращена к значению по умолчанию 15 ms. Из 960 запросов 726 завершились и 234 были отклонены. `InferenceFailureRatioHigh` перешёл pending→firing; оба правила позднее вернулись в inactive. `InferenceTTFCHigh` уже был firing при первой выборке, поэтому его исходное срабатывание не приписывается этому опыту. Доказательства: `results/hpa-timeline.jsonl`, `results/hpa-alerts.jsonl`, `results/hpa-alert-clear.json`.

## Метрики и формулы

Времена клиента измеряются `perf_counter`. TTFC — отправка запроса → первый `chunk`; полная длительность — отправка → `done`; inter-chunk latency — разница времени соседних `chunk`. Это не TTFT/TPOT/tokens/s: backend создаёт симулированные части, а не токены модели. В p50/p95 включаются только целиком завершённые ответы; рядом сохраняются число успешных наблюдений, отправленные и запланированные запросы, доли ошибок/timeout, rejected и aborted.

Classic Histograms сохраняют `le` и bounded label `prefix_cache`; Pod/version добавляются discovery. Примеры PromQL:

```promql
histogram_quantile(0.95, sum by (le, pod, prefix_cache) (rate(inference_ttfc_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le, pod, prefix_cache) (rate(inference_response_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le, pod, prefix_cache) (rate(inference_inter_chunk_seconds_bucket[5m])))
sum by (pod) (inference_running)
sum by (pod) (inference_waiting)
sum by (pod) (rate(inference_requests_total{outcome="completed"}[1m]))
sum by (pod, outcome) (rate(inference_requests_total{outcome=~"rejected|aborted"}[1m]))
```

Сверяйте Prometheus с клиентским CSV по одному run ID, тому же времени и только завершённым request ID. Request/session ID присутствуют в JSONL logs, но не используются как метки.

Чтобы пересоздать четыре проверенных SVG-графика:

```powershell
.\.venv\Scripts\python.exe .\plot_results.py `
  --warmup-timeline .\results\bad-probe-timeline.jsonl `
  --timeline .\results\hpa-timeline.jsonl `
  --requests .\results\b-bal-n-01.csv .\results\b-bal-n-02.csv .\results\b-bal-n-03.csv `
             .\results\b-bal-a-01.csv .\results\b-bal-a-02.csv .\results\b-bal-a-03.csv `
             .\results\b-hot-n-01.csv .\results\b-hot-n-02.csv .\results\b-hot-n-03.csv `
             .\results\b-hot-a-01.csv .\results\b-hot-a-02.csv .\results\b-hot-a-03.csv `
  --hpa-request .\results\hpa-load-01.csv `
  --output .\results\plots
```

Скрипт строит Ready/EndpointSlice timeline, p95 TTFC и очереди по Pod, средний p95 из трёх прогонов каждой политики и два синхронных временных ряда HPA replicas/load. Для readiness и HPA передаются отдельные timelines; он завершится с ошибкой, если нет трёх прогонов на комбинацию или фактического 2→3→2. Графики записываются в `results/plots/`.

## Интеграционные проверки Kubernetes

`tests/test_kubernetes_smoke.py` по умолчанию пропускается. Для каждого опыта задайте URL и ожидаемые версию/SHA; для проверки прогрева нужен port-forward к Pod, созданному только что, а для восстановления — URL нового Pod:

```powershell
$env:HW11_WARMING_URL = "http://127.0.0.1:18080"
$env:HW11_SERVICE_URL = "http://127.0.0.1:8080"
$env:HW11_REPLACEMENT_URL = "http://127.0.0.1:18081"
$env:HW11_EXPECTED_MODEL_VERSION = "sim-v1"
$env:HW11_EXPECTED_ARTIFACT_SHA256 = "<SHA-256 из /ready>"
$env:HW11_PRE_REPLACE_POD = "<старое имя Pod>"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_kubernetes_smoke.py" -v
```

Подготовьте `HW11_REPLACEMENT_URL`, направив отдельный `kubectl port-forward pod/<новый-pod> 18081:8080`. Эти проверки обращаются непосредственно к сервису в кластере и не заменяются unit-тестами.

## Ресурсы и удаление

Пределы приложения: CPU request 250m/limit 1, RAM request 128 Mi/limit 256 Mi на Pod; 2–3 реплики. Prometheus: 100m/192 Mi request, 500m/512 Mi limit, retention 24 h/512 MB и `emptyDir` 600 Mi. Grafana: 50m/96 Mi request, 250m/256 Mi limit и `emptyDir` 128 Mi. Metrics-server и kube-state-metrics добавляют свои компоненты. Измерьте фактический размер собранного образа командой `docker image inspect hw11-simulator:0.1.0 --format '{{.Size}}'`; не включайте эти CPU-имитации в оценку реальной модели.

Удаление собственного стенда:

```powershell
helm uninstall hw11 -n hw11
kubectl delete namespace hw11
kind delete cluster --name hw11
docker image rm hw11-simulator:0.1.0
docker compose down -v
```

Не удаляйте namespace, kind cluster или образы, если ими пользуются другие проекты.

## Сдача и ограничение текущей проверки

`report.html` — честный черновик: код, локальные тесты и стендовые YAML готовы, но он не содержит вымышленных результатов Kubernetes. В текущем Windows-сеансе не были доступны Docker, kind, kubectl и Helm; гипервизор выключен. Для фактического подтверждения прогревов, rollout/rollback, балансировки, HPA и четырёх графиков запустите команды выше после подготовки окружения.

В приложенном репозитории отсутствует `lecture11-extended.html`, на который ссылается задание. Поэтому точную компанию/кейс из лекции и её первичный источник для ревью нужно добавить после получения этой лекции; в `report.html` это явно отмечено.

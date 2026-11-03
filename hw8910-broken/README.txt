ДЗ 8+9+10 — воспроизводимый выпуск и API

Требуется Python 3.12. Быстрый stub-стенд и release-контракт не загружают
модель. Для тестов HTTP API установите requirements-fast.txt; для Prefect-flow
дополнительно requirements-orchestration.txt. Тяжёлые веса и генератор отделены
в requirements-model.txt. Все прямые зависимости зафиксированы.

Быстрые тесты:
  py -3.12 -m pip install -r requirements-fast.txt
  py -3.12 -m unittest discover -s tests -v

Независимый синтетический опыт (каталог нельзя переиспользовать для init):
  py -3.12 lab.py --workspace .lab-first init
  py -3.12 lab.py --workspace .lab-first run --fail-after-prepare
  py -3.12 lab.py --workspace .lab-first run
  py -3.12 prefect_flow.py --workspace .lab-flow run

Восстановление использует content-addressed кеш на диске: SHA-256 байтов входа
и канонического JSON всех настроек подготовки. Принятая версия записывается
атомарно; журнал хранит уникальные идентификаторы выпусков и указывает на
текущий принятый id. Однописательский режим задан контрактом.

Интеграция с существующим ДЗ 6+7 (без нового sweep и без подмены качества):
  py -3.12 hw67_integration.py --hw67-root ..\\hw67-broken --workspace .release
  py -3.12 prefect_flow.py --workspace .release-flow hw67 --hw67-root ..\\hw67-broken

Обвязка проверяет сохранённые baseline/candidate reports и сырые ответы,
пересчитывает метрики и полный quality_gate из evaluation/solution.py,
сверяет gate с сохранённым, проверяет MLflow run ID, затем строит manifest.
Она сохраняет копии полных отчётов и ответов с SHA-256. Модельный hash строится
по фактическим файлам выбранного локального MLflow adapter artifact; API
пересчитывает file hash при загрузке. Команда не обучает и не запускает sweep.

Для HTTP stub:
  py -3.12 api_server.py --workspace .lab-first --backend stub --port 8091

Для реальной модели после успешного build и установки requirements-model.txt:
  py -3.12 api_server.py --workspace .release --backend model --hw67-root ..\\hw67-broken --port 8091

Реальный smoke и небольшой замер (4 запроса последовательно и 4 при
concurrency=2 на медленном CPU):
  py -3.12 benchmark_api.py --workspace .release --backend model --hw67-root ..\\hw67-broken --requests 4
Результат сохраняется в tests/evidence/model-benchmark.json. Запрос прогрева
исключён из выборки; замер локальный, одна модель сериализует генерацию и не
является production capacity test.

В реальном режиме используется версия, зафиксированная в accepted.json. При
старте загружается именно она; изменение accepted.json не меняет живой backend,
нужен управляемый перезапуск. GET /debug доступен только в учебном HTTP stub
и в FastAPI только при явном enable_debug=True. Реальный API debug-маршрут
не регистрирует. /health означает, что процесс жив; /ready и генерация дают
503 до конца прогрева. POST /generate принимает непустой prompt и целое
max_new_tokens от 1 до 32.

Исполняемый HTTP-прототип service.py оставлен для стандартно-библиотечных
subprocess-тестов. FastAPI находится в api.py, приложение создаётся через
create_app с внедряемой фабрикой backend. StubBackend — только транспортная
фикстура, её ответы STUB ONLY не являются результатом модели или evaluation.

Полный набор из задания HW 6+7 находится отдельно в ../hw67-broken; его tests/
не переписывались. В Git не добавляются веса, модельный кеш, окружение, секреты
или .release workspaces. CI-шаблон ci.example.yml прогоняет быстрые тесты;
без удалённого CI run защита merge не заявляется. Результаты экспериментов и
ограничения заполнены в REPORT.txt; ссылка на видеозапись остаётся для автора.

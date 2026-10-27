# Итог выполнения ДЗ 6+7

## Данные и оценочный протокол

- Train/val-тензоры взяты из ДЗ 4/5; SHA256 train
  `AB914E0E2112063DF06D8D1C02BC1B7685DE7E47D4548F641752F14B334165F5`,
  val `C742CF1B2ABA9898F864E7D688729A0080FF7F6C258EB2296E15A076D71E0E8C`.
- Golden содержит 50 вопросов из независимого test split ДЗ 3 (SQuAD v1.1,
  CC BY-SA 4.0); training index содержит 2 657 записей. Пересечений ID/group
  нет. Ни один distractor не найден буквально в соответствующем контексте.
- Проверены формулировки и все варианты 50 вопросов: неоднозначных вариантов,
  поддерживаемых контекстом наравне с правильным, не выявлено. Критические
  срезы: `Steam_engine` (17), `Doctor_Who` (17), `Geology` (16); behavioral
  набор — 3 кейса.
- Подготовка данных и проверка leakage описаны в
  [`scripts/prepare_eval_data.py`](../scripts/prepare_eval_data.py) и
  [`evaluation/params.json`](../evaluation/params.json).

## Sweep и выбор кандидата

Выполнены шесть отдельных FINISHED MLflow runs по 4 optimizer steps, на одном
бюджете train=32 / val=8. Базовый val loss — 1.2849. Лучший результат у
`all-r8-low`: val loss 0.4647, 5 046 272 обучаемых параметров, адаптер 30.2 MiB,
пиковое наблюдаемое выделение CUDA-памяти 2 372 MiB. Сравнение всех кандидатов
и run ID находится в [`comparison.csv`](./comparison.csv).

Базовая модель закреплена на revision
`c1899de289a04d12100db370d81485cdf75e47ca`. Полные provenance-теги включают
SHA256 кода, конфигурации и train/val. Для чтения/записи YAML, JSON и MLflow
артефактов явно задан UTF-8, чтобы системная кодировка Windows не искажала
русскоязычные параметры.

## Baseline vs candidate и release

| Метрика | Baseline | Candidate |
|---|---:|---:|
| Accuracy на golden | 20/50 (40%) | 41/50 (82%) |
| Строгий JSON format rate | 22/50 (44%) | 42/50 (84%) |
| Behavioral | 2/3 | 3/3 |
| Ошибки inference | 0 | 0 |

| Критический срез | Baseline | Candidate |
|---|---:|---:|
| Steam_engine | 6/17 (35.3%) | 13/17 (76.5%) |
| Doctor_Who | 6/17 (35.3%) | 14/17 (82.4%) |
| Geology | 8/16 (50.0%) | 14/16 (87.5%) |

Quality gate пройден. Между моделями 22 улучшенных, 1 регрессировавший и
27 неизменившихся ответа. Единственная регрессия — вопрос о спин-оффе Doctor
Who: baseline ответил `C`, candidate отказался (`REFUSE`). Общая accuracy,
format rate, каждый критический срез и behavioral-проверка при этом улучшились.
Отрицательный тест подтвердил, что испорченные ответы отклоняются и не меняют
alias.

Версия `my-qwen-adapter:2` назначена alias `candidate` и, после успешного gate,
alias `champion`. Оба alias проверены через MLflow Registry и указывают на run
`efa9f05aded243eeb61aa097d7a881da`.

## Проверки и оставшиеся ограничения

- `python -m unittest discover -s tests -v`: все 24 теста пройдены.
- Реальная проверка `tests/check.py`: 9/9; пройдены sweep/provenance, артефакты,
  параметры адаптера, negative gate, загрузка candidate в новом изолированном
  процессе без локального конфига и проверка persistence.
- Фактические sweep/evaluation отчёты, сырые ответы и release-запись скопированы
  в `submission/evaluation-reports/`, чтобы их можно было приложить отдельно от
  игнорируемого `metrics/`.
- Docker CLI недоступен, поэтому `tests/check_storage.py` выполнил fallback:
  записал артефакт, остановил отдельный локальный MLflow server и проверил его
  SHA256 после запуска сервера заново на том же SQLite backend и файловом
  artifact store. `metrics/storage-check.json` фиксирует `mode=local-server-restart`.
  Это подтверждает сохранность между перезапусками сервера, но не заменяет
  проверку named Docker volume после пересоздания контейнера.
- Использован уже установленный runtime: torch 2.11.0+cu128,
  transformers 5.18.0, peft 0.21.2; версии отличаются от закреплённых в
  `pyproject.toml`/MLflow environment (2.14.0, 5.17.0, 0.21.0). Загрузка и
  inference проверены в текущем окружении, но проверка в точном lock-окружении
  не выполнялась.

Исправленные дефекты стартового проекта описаны в [`defects.md`](./defects.md).

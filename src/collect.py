"""Стадия collect: источник → data/raw.jsonl.

ЗДЕСЬ студент подменяет сбор на свой. Ниже — чтение parquet курсового датасета
НМО; у вас на этом месте будет парсер сайта, выгрузка из БД, экспорт из Notion.
Контракт стадии, а не её внутренности, держит остальной пайплайн:
на выходе JSONL со строками {"id", "topic", "messages": [system, user, assistant]}.

Скачанный чужой набор сам по себе сдачей не является (README, «Готовый датасет
как источник»). Поэтому стадия не перекладывает parquet в JSONL один в один,
а делает три вещи, и каждая видна числом в metrics/collect.json:

  1. сужает набор до перечисленных тем (collect.topics), если это нужно задаче;
  2. сверяет ответ с разметкой источника (collect.verify_answer_index) —
     расхождение выбрасывается, а не переносится в обучение;
  3. разводит единственную инструкцию источника на варианты
     (collect.system_prompts), чтобы модель не заучила её формулировку.
"""

import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

from src.config import load_params, source_files

def pick_prompt(example_id: str, variants: list[str]) -> str:
    """Детерминированно выбрать вариант инструкции по id примера.

    Именно sha1, а не встроенный hash(): тот солится на каждый запуск процесса,
    и raw.jsonl переставал бы быть воспроизводимым.
    """
    digest = hashlib.sha1(example_id.encode("utf-8")).hexdigest()
    return variants[int(digest, 16) % len(variants)]


def iter_squad(path: Path):
    """Прочитать официальный SQuAD JSON и отдать вопросы с ответами."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    for article in payload["data"]:
        topic = article["title"].strip()
        for paragraph in article["paragraphs"]:
            context = " ".join(paragraph["context"].split())
            for qa in paragraph["qas"]:
                answers = qa.get("answers", [])
                if not qa.get("id") or not qa.get("question") or not answers:
                    continue
                answer = answers[0].get("text", "").strip()
                if answer:
                    yield {"id": qa["id"], "topic": topic or "untitled",
                           "user": f"Context:\n{context}\n\nQuestion:\n{qa['question'].strip()}",
                           "assistant": answer}


def select_rows(rows: list[dict], limit: int) -> list[dict]:
    """Взять квоту равномерно по темам, не привязываясь к порядку источника."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["topic"]].append(row)
    selected: list[dict] = []
    topics = iter(sorted(grouped))
    active = list(topics)
    while active and len(selected) < limit:
        next_active: list[str] = []
        for topic in active:
            selected.append(grouped[topic].pop(0))
            if len(selected) >= limit:
                break
            if grouped[topic]:
                next_active.append(topic)
        active = next_active
    return selected


def main() -> None:
    params = load_params()
    cfg = params["collect"]
    paths = params["paths"]
    n_rows = cfg["n_rows"]
    variants = cfg["system_prompts"]
    if not variants:
        raise SystemExit("collect.system_prompts пуст: инструкцию брать неоткуда")
    topics = cfg["topics"]
    wanted = set(topics) if topics else None

    out = Path(paths["raw"])
    out.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    scanned = written = dropped_topic = dropped_answer = 0
    prompts_used: set[str] = set()

    with out.open("w", encoding="utf-8") as fh:
        for src in source_files(params):
            if not src.exists():
                raise SystemExit(f"нет файла-источника: {src}")
            candidates = []
            # Квота применяется после фильтров и балансировки по темам: порядок
            # статей в публичном источнике не должен определять состав датасета.
            for row in iter_squad(src):
                scanned += 1
                if wanted is not None and row["topic"] not in wanted:
                    dropped_topic += 1
                    continue
                candidates.append(row)
            for row in select_rows(candidates, n_rows):
                prompt = pick_prompt(row["id"], variants)
                prompts_used.add(prompt)
                record = {
                    "id": row["id"],
                    "topic": row["topic"],
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": row["user"]},
                        {"role": "assistant", "content": row["assistant"]},
                    ],
                }
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1

    metrics = {
        "version": cfg["version"],
        "files": len(source_files(params)),
        "rows_scanned": scanned,
        "rows_written": written,
        "dropped_topic_filter": dropped_topic,
        "dropped_answer_mismatch": dropped_answer,
        "topics_filter": len(wanted) if wanted else 0,
        "system_prompt_variants": len(prompts_used),
        "seconds": round(time.perf_counter() - started, 2),
    }
    mpath = Path(paths["metrics_collect"])
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"collect: версия {cfg['version']}, файлов {metrics['files']}, "
        f"просмотрено {scanned}, записано {written} "
        f"(фильтр тем -{dropped_topic}, расхождение с разметкой -{dropped_answer}), "
        f"вариантов инструкции {len(prompts_used)}, "
        f"{metrics['seconds']} с → {out}"
    )


if __name__ == "__main__":
    main()

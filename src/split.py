"""Стадия split: разбиение на train/val/test."""

import json
import random
import time
from pathlib import Path

from src.config import load_params
from src.contamination import report
from src.schema import Example, dump, iter_examples
from src.textnorm import normalize_group


def group_split(examples: list[Example], ratios: dict[str, float], seed: int) -> list[str]:
    """Назначить целые группы сплитам, сохраняя целостность тем."""
    grouped: dict[str, list[int]] = {}
    for index, example in enumerate(examples):
        grouped.setdefault(normalize_group(example.topic), []).append(index)
    groups = list(grouped.items())
    random.Random(seed).shuffle(groups)
    targets = {name: len(examples) * ratio for name, ratio in ratios.items()}
    assigned = {name: 0 for name in ratios}
    labels = [""] * len(examples)
    for _, indexes in groups:
        name = min(ratios, key=lambda candidate: assigned[candidate] / max(targets[candidate], 1))
        for index in indexes:
            labels[index] = name
        assigned[name] += len(indexes)
    return labels


def main() -> None:
    params = load_params()
    paths = params["paths"]
    cfg = params["split"]
    started = time.perf_counter()

    examples: list[Example] = list(iter_examples(paths["clean"]))
    if cfg["group_key"] != "topic":
        raise SystemExit(f"неизвестный split.group_key: {cfg['group_key']!r}")

    sizes: dict[str, int] = {}
    for ex in examples:
        key = normalize_group(ex.topic)
        sizes[key] = sizes.get(key, 0) + 1

    labels = group_split(examples, cfg["ratios"], cfg["seed"])
    buckets: dict[str, list[Example]] = {name: [] for name in cfg["ratios"]}
    for label, ex in zip(labels, examples):
        buckets[label].append(ex)

    for name, rows in buckets.items():
        out = Path(paths[name])
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for ex in rows:
                fh.write(dump(ex) + "\n")

    nd = params["clean"]["near_dup"]
    rep = report(
        buckets["train"],
        buckets["test"],
        shingle_words=nd["shingle_words"],
        num_perm=nd["num_perm"],
        threshold=params["contamination"]["threshold"],
    )

    metrics = {
        "version": params["collect"]["version"],
        "seed": cfg["seed"],
        "group_key": cfg["group_key"],
        "groups_total": len(sizes),
        "sizes": {name: len(rows) for name, rows in buckets.items()},
        "groups": {
            name: len({normalize_group(ex.topic) for ex in rows}) for name, rows in buckets.items()
        },
        "ratios_actual": {
            name: round(len(rows) / len(examples), 4) for name, rows in buckets.items()
        },
        "contamination": rep,
        "seconds": round(time.perf_counter() - started, 2),
    }
    mpath = Path(paths["metrics_split"])
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "split: "
        + ", ".join(f"{name} {len(rows)}" for name, rows in buckets.items())
        + f" (групп {len(sizes)}, {metrics['seconds']} с)"
    )


if __name__ == "__main__":
    main()

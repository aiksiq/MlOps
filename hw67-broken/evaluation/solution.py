"""Реализуйте две функции. Контракт и примеры: EVALUATION.md и tests/test_eval.py."""
import json
import math


_METRICS = ("n", "correct", "valid", "errors", "accuracy", "format_rate")


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющийся ключ JSON")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Недопустимая JSON-константа: {value}")


def _metrics(items):
    n = len(items)
    correct = sum(item["correct"] for item in items)
    valid = sum(item["valid"] for item in items)
    errors = sum(item["error"] is not None for item in items)
    return {
        "n": n,
        "correct": correct,
        "valid": valid,
        "errors": errors,
        "accuracy": correct / n,
        "format_rate": valid / n,
    }


def _check_metric_block(actual, expected, label):
    if not isinstance(actual, dict):
        raise ValueError(f"{label}: отсутствуют метрики")
    for key in ("n", "correct", "valid", "errors"):
        value = actual.get(key)
        if type(value) is not int or value != expected[key]:
            raise ValueError(f"{label}: некорректный счётчик {key}")
    for key in ("accuracy", "format_rate"):
        value = actual.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not math.isclose(
                    value, expected[key], rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError(f"{label}: некорректная метрика {key}")


def _validate_scored_report(report, label, min_n):
    if not isinstance(report, dict):
        raise ValueError(f"{label}: отсутствует отчёт")
    overall = report.get("overall")
    per_item = report.get("per_item")
    slices = report.get("slices")
    if not isinstance(per_item, list) or len(per_item) < min_n:
        raise ValueError(f"{label}: недостаточно примеров")

    item_by_id = {}
    for item in per_item:
        if not isinstance(item, dict):
            raise ValueError(f"{label}: некорректная запись per_item")
        if not {"id", "raw", "parsed", "valid", "correct", "error", "slices"} <= set(item):
            raise ValueError(f"{label}: неполная запись per_item")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in item_by_id:
            raise ValueError(f"{label}: пустой или повторяющийся id")
        if not isinstance(item["raw"], str):
            raise ValueError(f"{label}: raw-ответ должен быть строкой")
        if type(item.get("valid")) is not bool or type(item.get("correct")) is not bool:
            raise ValueError(f"{label}: некорректные флаги valid/correct")
        if item["correct"] and not item["valid"]:
            raise ValueError(f"{label}: невалидный ответ отмечен верным")
        if item["parsed"] is not None and not isinstance(item["parsed"], str):
            raise ValueError(f"{label}: parsed должен быть строкой или null")
        if item["valid"] != (item["parsed"] is not None):
            raise ValueError(f"{label}: valid не соответствует parsed")
        error = item.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError(f"{label}: некорректное поле error")
        item_slices = item.get("slices")
        if (not isinstance(item_slices, list) or not item_slices
                or any(not isinstance(name, str) or not name for name in item_slices)
                or len(item_slices) != len(set(item_slices))):
            raise ValueError(f"{label}: некорректные slices")
        item_by_id[item_id] = item

    expected_overall = _metrics(per_item)
    _check_metric_block(overall, expected_overall, f"{label}.overall")
    if expected_overall["n"] < min_n:
        raise ValueError(f"{label}: недостаточно примеров")

    if not isinstance(slices, dict):
        raise ValueError(f"{label}: отсутствуют срезы")
    expected_slice_names = {name for item in per_item for name in item["slices"]}
    if set(slices) != expected_slice_names:
        raise ValueError(f"{label}: имена срезов не соответствуют per_item")
    for name in expected_slice_names:
        subset = [item for item in per_item if name in item["slices"]]
        _check_metric_block(slices[name], _metrics(subset), f"{label}.slices.{name}")
    return item_by_id, expected_overall, slices


def score(rows, outputs):
    """Верните overall, slices и per_item. Не удаляйте плохие ответы из знаменателя.

    Строгий ответ: JSON с единственным строковым полем answer из choices.
    outputs: [{id, raw, error, latency_seconds}, ...].
    В overall и каждом срезе: n, correct, valid, errors, accuracy, format_rate.
    В per_item: id, raw, parsed, valid, correct, error, slices.
    Несовпадающие/дублирующиеся id и пустой набор должны вызвать ValueError.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("Набор оценки не должен быть пустым")
    if not isinstance(outputs, list):
        raise ValueError("Ответы должны быть списком")

    rows_by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Некорректная строка набора оценки")
        item_id = row.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in rows_by_id:
            raise ValueError("Пустой или повторяющийся id задания")
        choices = row.get("choices")
        slices = row.get("slices")
        expected = row.get("expected")
        if (not isinstance(choices, list) or not choices
                or any(not isinstance(choice, str) for choice in choices)
                or len(choices) != len(set(choices))
                or not isinstance(expected, str) or expected not in choices):
            raise ValueError(f"{item_id}: некорректные choices или expected")
        if (not isinstance(slices, list) or not slices
                or any(not isinstance(name, str) or not name for name in slices)
                or len(slices) != len(set(slices))):
            raise ValueError(f"{item_id}: некорректные slices")
        rows_by_id[item_id] = row

    outputs_by_id = {}
    for output in outputs:
        if not isinstance(output, dict):
            raise ValueError("Некорректная запись ответа")
        output_id = output.get("id")
        if not isinstance(output_id, str) or not output_id or output_id in outputs_by_id:
            raise ValueError("Пустой или повторяющийся id ответа")
        if "raw" not in output or "error" not in output or not isinstance(output["raw"], str):
            raise ValueError(f"{output_id}: отсутствует raw-ответ или поле error")
        error = output.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError(f"{output_id}: некорректное поле error")
        outputs_by_id[output_id] = output

    if set(rows_by_id) != set(outputs_by_id):
        raise ValueError("ID заданий и ответов не совпадают")

    per_item = []
    slice_items = {}
    for row in rows:
        output = outputs_by_id[row["id"]]
        raw = output.get("raw")
        error = output.get("error")
        parsed = None
        valid = False
        if error is None and isinstance(raw, str):
            try:
                payload = json.loads(
                    raw,
                    object_pairs_hook=_strict_object,
                    parse_constant=_reject_constant,
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = None
            if (isinstance(payload, dict) and set(payload) == {"answer"}
                    and isinstance(payload["answer"], str)
                    and payload["answer"] in row["choices"]):
                parsed = payload["answer"]
                valid = True
        item_slices = list(row["slices"])
        item = {
            "id": row["id"],
            "raw": raw,
            "parsed": parsed,
            "valid": valid,
            "correct": valid and parsed == row["expected"],
            "error": error,
            "slices": item_slices,
        }
        per_item.append(item)
        for name in item_slices:
            slice_items.setdefault(name, []).append(item)

    return {
        "overall": _metrics(per_item),
        "slices": {name: _metrics(items) for name, items in sorted(slice_items.items())},
        "per_item": per_item,
    }


def quality_gate(baseline, candidate, critical_slices):
    """Верните {passed: True, ...} или вызовите ValueError с причиной отказа.

    Проверьте совместимость протокола/набора/baseline, метрики и counts,
    все id и срезы, общую accuracy/format_rate, критические срезы и behavior.
    Missing, NaN, пустой набор и ошибки генерации означают отказ.
    Пороги задаются до измерения; учебный допуск регрессии равен нулю.
    """
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("Отсутствует отчёт baseline или candidate")
    for key in ("protocol_sha256", "golden_sha256", "baseline_spec_sha256"):
        left, right = baseline.get(key), candidate.get(key)
        if not isinstance(left, str) or not left or left != right:
            raise ValueError(f"Несовместимый или отсутствующий {key}")
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        if report.get("status") != "complete":
            raise ValueError(f"{label}: оценка не завершена")
        if not isinstance(report.get("model"), dict) or not report["model"]:
            raise ValueError(f"{label}: отсутствует конкретная модель")

    baseline_items, baseline_overall, baseline_slices = _validate_scored_report(
        baseline, "baseline", 50
    )
    candidate_items, candidate_overall, candidate_slices = _validate_scored_report(
        candidate, "candidate", 50
    )
    if len(baseline_slices) < 3 or len(candidate_slices) < 3:
        raise ValueError("Golden должен содержать минимум три среза")
    if set(baseline_items) != set(candidate_items):
        raise ValueError("ID baseline и candidate не совпадают")
    for item_id in baseline_items:
        if set(baseline_items[item_id]["slices"]) != set(candidate_items[item_id]["slices"]):
            raise ValueError(f"{item_id}: срезы baseline и candidate не совпадают")
    if set(baseline_slices) != set(candidate_slices):
        raise ValueError("Имена срезов baseline и candidate не совпадают")
    for name in baseline_slices:
        if baseline_slices[name]["n"] != candidate_slices[name]["n"]:
            raise ValueError(f"{name}: размеры срезов baseline и candidate не совпадают")

    if baseline_overall["errors"] or candidate_overall["errors"]:
        raise ValueError("Ошибка генерации в golden-оценке")
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        behavior = report.get("behavioral")
        behavior_items, behavior_overall, _ = _validate_scored_report(
            behavior, f"{label}.behavioral", 3
        )
        if label == "baseline":
            baseline_behavior_items = behavior_items
        else:
            candidate_behavior_items = behavior_items
        if behavior_overall["errors"]:
            raise ValueError(f"{label}: ошибка генерации behavioral-кейса")

    if set(baseline_behavior_items) != set(candidate_behavior_items):
        raise ValueError("ID behavioral-кейсов baseline и candidate не совпадают")
    for item_id in baseline_behavior_items:
        if set(baseline_behavior_items[item_id]["slices"]) != set(candidate_behavior_items[item_id]["slices"]):
            raise ValueError(f"{item_id}: behavioral-срезы не совпадают")
    if behavior_overall["correct"] != behavior_overall["n"] or behavior_overall["valid"] != behavior_overall["n"]:
        raise ValueError("Не все обязательные behavioral-кейсы прошли")
    candidate_behavior = candidate["behavioral"]["overall"]
    if candidate_behavior["correct"] != candidate_behavior["n"] or candidate_behavior["valid"] != candidate_behavior["n"]:
        raise ValueError("Не все behavioral-кейсы candidate прошли")

    if candidate_overall["accuracy"] < baseline_overall["accuracy"]:
        raise ValueError("Accuracy candidate ниже baseline")
    if candidate_overall["format_rate"] < baseline_overall["format_rate"]:
        raise ValueError("Format rate candidate ниже baseline")
    if not isinstance(critical_slices, list) or not critical_slices:
        raise ValueError("Не заданы критические срезы")
    if (any(not isinstance(name, str) or not name for name in critical_slices)
            or len(critical_slices) != len(set(critical_slices))):
        raise ValueError("Некорректный список критических срезов")
    for name in critical_slices:
        if name not in baseline_slices or name not in candidate_slices:
            raise ValueError(f"Критический срез отсутствует: {name}")
        if candidate_slices[name]["accuracy"] < baseline_slices[name]["accuracy"]:
            raise ValueError(f"Accuracy candidate ухудшилась в критическом срезе {name}")

    return {
        "passed": True,
        "n": candidate_overall["n"],
        "baseline_accuracy": baseline_overall["accuracy"],
        "candidate_accuracy": candidate_overall["accuracy"],
        "baseline_format_rate": baseline_overall["format_rate"],
        "candidate_format_rate": candidate_overall["format_rate"],
        "critical_slices": {
            name: {
                "baseline_accuracy": baseline_slices[name]["accuracy"],
                "candidate_accuracy": candidate_slices[name]["accuracy"],
                "n": candidate_slices[name]["n"],
            }
            for name in critical_slices
        },
        "behavioral_passed": candidate_behavior["correct"],
    }

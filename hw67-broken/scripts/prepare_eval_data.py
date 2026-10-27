"""Prepare a deterministic multiple-choice audit set from the held-out split."""
import argparse
import json
import random
import re
from pathlib import Path


TOPIC_QUOTAS = {
    "Steam_engine": 17,
    "Doctor_Who": 17,
    "Geology": 16,
}
LABELS = ("A", "B", "C", "D")
CHOICES = [*LABELS, "REFUSE"]
SEED = 42


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as file:
        rows = [json.loads(line) for line in file if line.strip()]
    ids = [row["id"] for row in rows]
    if not rows or len(ids) != len(set(ids)):
        raise ValueError(f"{path}: empty data or duplicate ids")
    return rows


def normalize(text):
    return " ".join(text.casefold().split())


def user_content(row):
    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) < 3:
        raise ValueError(f"{row.get('id')}: expected system/user/assistant messages")
    if messages[1].get("role") != "user" or messages[-1].get("role") != "assistant":
        raise ValueError(f"{row.get('id')}: unexpected message roles")
    return messages[1]["content"].strip()


def answer_text(row):
    answer = row["messages"][-1]["content"].strip()
    if not answer or len(answer) > 160:
        raise ValueError(f"{row['id']}: empty or overlong source answer")
    return answer


def parse_context_question(text):
    match = re.search(r"\nQuestion:\s*", text)
    if match is None:
        raise ValueError("Source question has no Question: separator")
    context, question = text[:match.start()].strip(), text[match.end():].strip()
    if not context or not question:
        raise ValueError("Source context or question is empty")
    return context, question


def distractors(row, topic_rows, fallback_rows, context, rng):
    expected = normalize(answer_text(row))
    normalized_context = normalize(context)
    candidates = {}

    def add_candidate(candidate):
        if candidate["id"] == row["id"]:
            return
        text = candidate["messages"][-1]["content"].strip()
        if len(text) < 6 or len(text) > 160:
            return
        key = normalize(text)
        if key == expected or key in normalized_context:
            return
        expected_tokens = set(re.findall(r"\w+", expected))
        candidate_tokens = set(re.findall(r"\w+", key))
        if expected_tokens <= candidate_tokens or candidate_tokens <= expected_tokens:
            return
        candidates.setdefault(key, text)

    for candidate in topic_rows:
        add_candidate(candidate)
    if len(candidates) < 3:
        for candidate in fallback_rows:
            add_candidate(candidate)
            if len(candidates) >= 3:
                break

    options = list(candidates.values())
    if len(options) < 3:
        raise ValueError(f"{row['topic']}: not enough unique distractors")
    return rng.sample(options, 3)


def make_item(row, topic_rows, fallback_rows, rng):
    context, question = parse_context_question(user_content(row))
    expected_text = answer_text(row)
    options = distractors(row, topic_rows, fallback_rows, context, rng) + [expected_text]
    rng.shuffle(options)
    expected = LABELS[options.index(expected_text)]
    prompt = (
        f"{context}\n\nQuestion:\n{question}\n\n"
        "Choose the option supported by the passage. If the passage does not "
        "support an answer, choose REFUSE.\nChoices:\n"
        + "\n".join(f"{label}. {text}" for label, text in zip(LABELS, options))
        + "\nREFUSE. The passage does not provide the answer."
    )
    return {
        "id": f"gold-{row['id']}",
        "group_id": row["topic"],
        "input": prompt,
        "expected": expected,
        "choices": CHOICES,
        "slices": [row["topic"]],
        "source": (
            f"SQuAD v1.1, CC BY-SA 4.0; hw3-broken/data/test.jsonl; "
            f"source_id={row['id']}; "
            "expected copied from the held-out source answer"
        ),
    }


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", default="../hw4-broken/data/train.jsonl")
    parser.add_argument("--test", default="../hw3-broken/data/test.jsonl")
    parser.add_argument("--out", default="data/eval")
    args = parser.parse_args()

    training = read_jsonl(args.train)
    test = read_jsonl(args.test)
    training_ids = {row["id"] for row in training}
    training_topics = {row["topic"] for row in training}
    if training_ids.intersection(row["id"] for row in test):
        raise ValueError("Train and held-out test IDs overlap")
    if training_topics.intersection(row["topic"] for row in test):
        raise ValueError("Train and held-out test topics overlap")

    by_topic = {}
    for row in test:
        by_topic.setdefault(row["topic"], []).append(row)
    selected = []
    rng = random.Random(SEED)
    for topic, count in TOPIC_QUOTAS.items():
        rows = sorted(
            (
                row for row in by_topic.get(topic, [])
                if 0 < len(row["messages"][-1]["content"].strip()) <= 160
            ),
            key=lambda row: row["id"],
        )
        if len(rows) < count:
            raise ValueError(f"{topic}: need {count} test rows, found {len(rows)}")
        selected.extend(make_item(row, rows, test, rng) for row in rows[:count])

    if len(selected) != 50:
        raise ValueError("Golden set must contain exactly 50 rows")

    training_index = [
        {
            "id": row["id"],
            "group_id": row["topic"],
            "input": user_content(row),
        }
        for row in training
    ]

    first, second = selected[0], selected[17]
    format_case = {
        **first,
        "id": "behavior-format",
        "type": "format",
        "group_id": "behavior-format",
        "source": "Held-out golden item; checks strict JSON response formatting",
    }
    context2, remainder = parse_context_question(second["input"])
    question2, _ = remainder.split("\n\nChoose the option", 1)
    options_text = second["input"].split("\nChoices:\n", 1)[1]
    paraphrase = {
        **second,
        "id": "behavior-paraphrase",
        "input": (
            f"{context2}\n\nQuestion:\nAccording to the passage, {question2.strip()}\n\n"
            "Choose the option supported by the passage. If the passage does not "
            "support an answer, choose REFUSE.\nChoices:\n"
            + options_text
        ),
        "type": "paraphrase",
        "group_id": second["group_id"],
        "source": "Paraphrase of held-out golden item " + second["id"],
    }
    refusal = {
        "id": "behavior-refusal",
        "group_id": "behavior-refusal",
        "input": (
            "Context:\nThe passage contains no information about this question.\n\n"
            "Question:\nWhat was the exact temperature in London at noon yesterday?\n\n"
            "Choose the option supported by the passage. If the passage does not "
            "support an answer, choose REFUSE.\nChoices:\n"
            "A. 0 degrees\nB. 10 degrees\nC. 20 degrees\nD. 30 degrees\n"
            "REFUSE. The passage does not provide the answer."
        ),
        "expected": "REFUSE",
        "choices": CHOICES,
        "slices": ["refusal"],
        "type": "refusal",
        "source": "Out-of-context behavioral case defined by the evaluation protocol",
    }
    behavioral = [format_case, paraphrase, refusal]

    out = Path(args.out)
    write_jsonl(out / "golden.jsonl", selected)
    write_jsonl(out / "training-index.jsonl", training_index)
    write_jsonl(out / "behavioral.jsonl", behavioral)
    print(
        f"Prepared {len(selected)} held-out golden rows, "
        f"{len(training_index)} training-index rows, "
        f"{len(behavioral)} behavioral cases"
    )


if __name__ == "__main__":
    main()

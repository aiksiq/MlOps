"""Re-check saved HW 6+7 outputs and bind their full gate to a release manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import lab
import release


_EVIDENCE_FILES = (
    "baseline-eval.json",
    "candidate-eval.json",
    "baseline-outputs.json",
    "candidate-outputs.json",
    "quality-gate.json",
)
_TOKENIZER_NAMES = {
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "tokenizer.model",
    "spiece.model",
    "vocab.json",
    "vocab.txt",
    "merges.txt",
}


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy_evidence(hw67_root, workspace):
    source = hw67_root / "metrics"
    evidence_root = workspace / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    evidence = {}
    for name in _EVIDENCE_FILES:
        source_path = source / name
        content = source_path.read_bytes()
        destination = evidence_root / name
        release.atomic_write_bytes(destination, content)
        evidence[name] = {
            "path": str(Path("evidence") / name),
            "sha256": _sha256(content),
        }
    return evidence


def _artifact_files(adapter_root):
    files = {}
    for path in sorted(adapter_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Adapter artifact must not contain symlinks: {path}")
        if path.is_file():
            files[path.relative_to(adapter_root).as_posix()] = _file_sha256(path)
    if not files or "adapter_config.json" not in files or not any(
        name.endswith((".safetensors", ".bin")) for name in files
    ):
        raise ValueError("Selected MLflow artifact does not contain adapter weights and config")
    return files


def _candidate_artifact(hw67_root, run_id):
    matches = list((hw67_root / "mlartifacts").glob(
        f"*/{run_id}/artifacts/model/artifacts/adapter"
    ))
    if len(matches) != 1 or not matches[0].is_dir():
        raise FileNotFoundError(f"Expected one local adapter artifact for MLflow run {run_id}")
    return matches[0].resolve()


def _score_saved_outputs(hw67_root):
    sys.path.insert(0, str(hw67_root))
    from evaluation.contract import fingerprint, read_json, read_rows, validate_golden
    from evaluation.solution import quality_gate, score

    config = read_json(hw67_root / "evaluation/params.json")
    baseline_spec = read_json(hw67_root / "evaluation/baseline.json")
    candidate_spec = read_json(hw67_root / "metrics/candidate-model.json")
    golden = read_rows(hw67_root / config["golden"])
    training = read_rows(hw67_root / config["training_index"])
    behavioral = read_rows(hw67_root / config["behavioral"])
    leakage = validate_golden(golden, training, behavioral)
    if config.get("max_new_tokens") != 32:
        raise ValueError("The HW 6+7 evaluation limit must match the service protocol (32)")
    if not isinstance(config.get("critical_slices"), list) or not config["critical_slices"]:
        raise ValueError("Evaluation must pin its critical slices")

    protocol = {
        "config": config,
        "golden": golden,
        "behavioral": behavioral,
        "training_index": training,
        "evaluation_code": {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted((hw67_root / "evaluation").glob("*.py"))
        },
    }
    protocol_hash = fingerprint(protocol)
    baseline = read_json(hw67_root / "metrics/baseline-eval.json")
    candidate = read_json(hw67_root / "metrics/candidate-eval.json")
    outputs_by_role = {
        role: read_json(hw67_root / f"metrics/{role}-outputs.json")
        for role in ("baseline", "candidate")
    }

    for role, report, spec in (
        ("baseline", baseline, baseline_spec),
        ("candidate", candidate, candidate_spec),
    ):
        if report.get("status") != "complete" or report.get("model") != spec:
            raise ValueError(f"{role}: saved report does not bind to its pinned model")
        if (
            report.get("protocol_sha256") != protocol_hash
            or report.get("golden_sha256") != fingerprint(golden)
            or report.get("baseline_spec_sha256") != fingerprint(baseline_spec)
        ):
            raise ValueError(f"{role}: data, protocol, baseline, or evaluation code changed")
        if report.get("leakage") != leakage:
            raise ValueError(f"{role}: saved leakage check does not match the current data")
        outputs = outputs_by_role[role]
        if fingerprint(outputs) != report.get("outputs_sha256"):
            raise ValueError(f"{role}: saved raw outputs changed")
        by_id = {output["id"]: output for output in outputs}
        if len(by_id) != len(outputs) or set(by_id) != {
            row["id"] for row in golden + behavioral
        }:
            raise ValueError(f"{role}: raw output IDs do not match the evaluation data")
        calculated = score(golden, [by_id[row["id"]] for row in golden])
        calculated_behavior = score(behavioral, [by_id[row["id"]] for row in behavioral])
        if any(report.get(key) != calculated[key] for key in ("overall", "slices", "per_item")):
            raise ValueError(f"{role}: saved metrics do not match the raw answers")
        if report.get("behavioral") != calculated_behavior:
            raise ValueError(f"{role}: behavioral metrics do not match the raw answers")
        report["_calculated"] = calculated
        report["_behavioral_calculated"] = calculated_behavior
        report["_outputs"] = outputs

    saved_gate = read_json(hw67_root / "metrics/quality-gate.json")
    try:
        result = quality_gate(baseline, candidate, config["critical_slices"])
        gate_failure = None
    except ValueError as exc:
        gate_failure = str(exc)
        result = {"passed": False, "reason": gate_failure}
    if saved_gate != result:
        raise ValueError("Saved quality-gate result differs from the recomputed full gate")
    if baseline.get("evaluation_run_id") != candidate.get("evaluation_run_id"):
        raise ValueError("Baseline and candidate were not evaluated in one run")
    candidate_model = candidate["model"]
    if candidate_model.get("run_id") != candidate_spec.get("run_id"):
        raise ValueError("Candidate evaluation is bound to another MLflow run")
    return {
        "config": config,
        "baseline_spec": baseline_spec,
        "candidate_spec": candidate_spec,
        "baseline": baseline,
        "candidate": candidate,
        "protocol_sha256": protocol_hash,
        "golden_sha256": fingerprint(golden),
        "quality_gate": result,
        "gate_failure": gate_failure,
        "leakage": leakage,
    }


def _create_manifest(hw67_root, verified):
    spec = verified["candidate_spec"]
    adapter_root = _candidate_artifact(hw67_root, spec["run_id"])
    adapter_metadata = _read_json(adapter_root / "manifest.json")
    model_config = adapter_metadata["model"]
    file_hashes = _artifact_files(adapter_root)
    tokenizer_hashes = {
        name: digest
        for name, digest in file_hashes.items()
        if Path(name).name in _TOKENIZER_NAMES
    }
    if not tokenizer_hashes or "chat_template.jinja" not in file_hashes:
        raise ValueError("Adapter is missing pinned tokenizer or chat template files")
    tokenizer_sha256 = _sha256(release.canonical_json(tokenizer_hashes))
    template_sha256 = file_hashes["chat_template.jinja"]

    dtype = os.environ.get("HW6_DTYPE", model_config["dtype"])
    generation = {
        "system": verified["config"]["system"],
        "do_sample": False,
        "seed": verified["config"]["seed"],
        "max_new_tokens": verified["config"]["max_new_tokens"],
        "enable_thinking": verified["config"]["enable_thinking"],
    }
    runtime = {
        "base_revision": model_config["revision"],
        "tokenizer_sha256": tokenizer_sha256,
        "template_sha256": template_sha256,
        "generation": generation,
        "dtype": dtype,
    }
    model_id = f"{spec['name']}/{spec['version']}@{spec['run_id']}"
    manifest = {
        "schema_version": 1,
        "model_id": model_id,
        "artifact_uri": spec["uri"],
        "model_sha256": _sha256(release.canonical_json(file_hashes)),
        "protocol_sha256": verified["protocol_sha256"],
        "golden_sha256": verified["golden_sha256"],
        "runtime_sha256": _sha256(release.canonical_json(runtime)),
        "runtime": runtime,
        "model_source": {
            "kind": "adapter",
            "name": model_config["name"],
            "revision": model_config["revision"],
            "device": os.environ.get("HW6_DEVICE", model_config.get("device", "auto")),
            "run_id": spec["run_id"],
            "adapter_path": str(adapter_root),
            "template_file": "chat_template.jinja",
        },
        "model_file_hashes": file_hashes,
        "tokenizer_file_hashes": tokenizer_hashes,
    }
    return manifest


def build_release(hw67_root, workspace):
    hw67_root = Path(hw67_root).resolve()
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    evidence = _copy_evidence(hw67_root, workspace)
    manifest = None
    failure = None
    try:
        verified = _score_saved_outputs(hw67_root)
        manifest = _create_manifest(hw67_root, verified)
        failure = verified["gate_failure"]
    except (ValueError, KeyError, FileNotFoundError) as exc:
        failure = str(exc)
        if manifest is None:
            raise

    candidate_path = hw67_root / "metrics/candidate-eval.json"
    candidate_eval = _read_json(candidate_path)
    candidate_model = candidate_eval["model"]
    if manifest is None:
        raise ValueError(failure or "Unable to build release manifest")
    passed = failure is None
    report = {
        "status": "complete" if passed else "rejected",
        "passed": passed,
        "evaluation_run_id": candidate_eval["evaluation_run_id"],
        "model_id": manifest["model_id"],
        "model_sha256": manifest["model_sha256"],
        "protocol_sha256": manifest["protocol_sha256"],
        "golden_sha256": manifest["golden_sha256"],
        "runtime_sha256": manifest["runtime_sha256"],
        "metrics": {
            "accuracy": candidate_eval["overall"]["accuracy"],
            "format_rate": candidate_eval["overall"]["format_rate"],
        },
        "evaluation": {
            "candidate_model": candidate_model,
            "quality_gate": evidence["quality-gate.json"],
            "baseline_eval": evidence["baseline-eval.json"],
            "candidate_eval": evidence["candidate-eval.json"],
            "baseline_outputs": evidence["baseline-outputs.json"],
            "candidate_outputs": evidence["candidate-outputs.json"],
            "leakage": verified["leakage"],
            "failure": failure,
        },
    }
    lab.write(workspace / "staged-manifest.json", manifest)
    lab.write(workspace / "evaluation-report.json", report)
    return manifest, report, passed


def publish_hw67_release(hw67_root, workspace):
    manifest, report, passed = build_release(hw67_root, workspace)
    if not passed:
        raise ValueError(report["evaluation"]["failure"])
    release.publish(manifest, report, Path(workspace) / "accepted.json")
    lab._record_publication(Path(workspace), manifest, report)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hw67-root", type=Path, default=Path("../hw67-broken"))
    parser.add_argument("--workspace", type=Path, default=Path(".release"))
    args = parser.parse_args()
    manifest = publish_hw67_release(args.hw67_root, args.workspace)
    print(json.dumps({
        "accepted": str(args.workspace / "accepted.json"),
        "model_id": manifest["model_id"],
        "runtime_sha256": manifest["runtime_sha256"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

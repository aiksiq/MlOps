"""Contracts for cache identity and atomic release publication."""
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BOUND_FIELDS = (
    "model_sha256",
    "protocol_sha256",
    "golden_sha256",
    "runtime_sha256",
)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def cache_key(data_path: Path, config: dict) -> str:
    if not isinstance(config, dict):
        raise ValueError("config must be a dictionary")
    with Path(data_path).open("rb") as stream:
        data_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return hashlib.sha256(canonical_json({"data_sha256": data_hash, "config": config})).hexdigest()


def _required_text(mapping: dict, key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


def validate_release(manifest: dict, report: dict) -> None:
    if not isinstance(manifest, dict) or not isinstance(report, dict):
        raise ValueError("manifest and report must be objects")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    _required_text(manifest, "model_id", "manifest")
    _required_text(manifest, "artifact_uri", "manifest")

    for field in _BOUND_FIELDS:
        value = manifest.get(field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(f"manifest.{field} must be a lowercase SHA-256 digest")

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or not runtime:
        raise ValueError("manifest.runtime must be a non-empty object")
    try:
        calculated_runtime_hash = hashlib.sha256(canonical_json(runtime)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest.runtime is not canonical JSON") from exc
    if calculated_runtime_hash != manifest["runtime_sha256"]:
        raise ValueError("manifest.runtime_sha256 does not match runtime")

    if report.get("status") != "complete" or report.get("passed") is not True:
        raise ValueError("evaluation report is incomplete or rejected")
    _required_text(report, "evaluation_run_id", "report")
    _required_text(report, "model_id", "report")
    if report["model_id"] != manifest["model_id"]:
        raise ValueError("report model_id does not match manifest")
    for field in _BOUND_FIELDS:
        if report.get(field) != manifest[field]:
            raise ValueError(f"report {field} does not match manifest")

    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("report.metrics must be an object")
    for field in ("accuracy", "format_rate"):
        value = metrics.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
            or not math.isfinite(value)
        ):
            raise ValueError(f"report.metrics.{field} must be a finite number in [0, 1]")


def atomic_write_bytes(target: Path, content: bytes) -> None:
    target = Path(target)
    temp_path = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp_path = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def publish(manifest: dict, report: dict, target: Path) -> None:
    validate_release(manifest, report)
    try:
        payload = (json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest is not valid JSON") from exc
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, payload)

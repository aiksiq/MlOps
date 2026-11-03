"""Measure local API latency with identical requests at concurrency one and two."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(url, payload, timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
            status = response.status
            error = None
    except HTTPError as exc:
        status = exc.code
        result = json.loads(exc.read().decode("utf-8"))
        error = result.get("detail", result.get("error", "HTTP failure"))
    except (OSError, URLError, TimeoutError) as exc:
        status, result, error = None, None, f"{type(exc).__name__}: {exc}"
    return {
        "status": status,
        "latency_seconds": time.perf_counter() - started,
        "server_generation_seconds": result.get("generation_seconds") if result else None,
        "output_tokens": result.get("output_tokens") if result else None,
        "request_id": result.get("request_id") if result else None,
        "raw_answer": result.get("answer") if result else None,
        "error": error,
    }


def _wait_for_api(process, base_url, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"API exited during startup:\n{output}")
        try:
            with urlopen(base_url + "/ready", timeout=2) as response:
                body = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and body.get("ready") is True:
                    return
        except (OSError, URLError, TimeoutError):
            time.sleep(0.2)
    raise TimeoutError(f"API did not become ready within {timeout} seconds")


def _batch(base_url, prompt, count, concurrency):
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        records = list(
            executor.map(
                lambda _: _request(
                    base_url + "/generate",
                    {"prompt": prompt, "max_new_tokens": 32},
                ),
                range(count),
            )
        )
    elapsed = time.perf_counter() - started
    latencies = [row["latency_seconds"] for row in records]
    return {
        "concurrency": concurrency,
        "requests": records,
        "errors": sum(row["error"] is not None for row in records),
        "batch_seconds": elapsed,
        "requests_per_second": count / elapsed if elapsed else 0,
        "average_latency_seconds": sum(latencies) / len(latencies),
        "max_latency_seconds": max(latencies),
    }


def _actual_device(manifest):
    device = manifest.get("model_source", {}).get("device", "stub")
    if device != "auto":
        return device
    import torch

    return (
        "cuda" if torch.cuda.is_available()
        else "mps" if torch.backends.mps.is_available()
        else "cpu"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path(".release"))
    parser.add_argument("--backend", choices=("model", "stub"), default="model")
    parser.add_argument("--hw67-root", type=Path, default=Path("../hw67-broken"))
    parser.add_argument("--requests", type=int, default=4)
    parser.add_argument("--warmup-timeout", type=int, default=300)
    parser.add_argument("--output", type=Path, default=Path("tests/evidence/model-benchmark.json"))
    args = parser.parse_args()
    if args.requests < 1:
        raise ValueError("requests must be positive")

    workspace = args.workspace.resolve()
    manifest = json.loads((workspace / "accepted.json").read_text(encoding="utf-8"))
    hw67_root = args.hw67_root.resolve()
    data_path = hw67_root / "data/eval/golden.jsonl"
    with data_path.open(encoding="utf-8") as stream:
        prompt = json.loads(next(stream))["input"]

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    command = [
        sys.executable,
        "api_server.py",
        "--workspace",
        str(workspace),
        "--backend",
        args.backend,
        "--hw67-root",
        str(hw67_root),
        "--port",
        str(port),
        "--warmup-seconds",
        "1",
    ]
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    server = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env=env,
    )
    started = time.perf_counter()
    try:
        _wait_for_api(server, base_url, args.warmup_timeout)
        startup_seconds = time.perf_counter() - started
        warmup = _request(
            base_url + "/generate",
            {"prompt": prompt, "max_new_tokens": 32},
        )
        if warmup["status"] != 200 or warmup["error"] is not None:
            raise RuntimeError(f"Warmup request failed: {warmup}")
        results = [
            _batch(base_url, prompt, args.requests, concurrency)
            for concurrency in (1, 2)
        ]
        output = {
            "model_id": manifest["model_id"],
            "runtime_sha256": manifest["runtime_sha256"],
            "backend": args.backend,
            "device": _actual_device(manifest) if args.backend == "model" else "stub",
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "max_new_tokens": 32,
            "warmup_seconds": startup_seconds,
            "warmup_request": warmup,
            "batches": results,
            "limitations": [
                "Local single-process measurement; it is not a production capacity test.",
                "One model instance serializes generation; concurrent latency includes queueing.",
            ],
        }
        target = args.output
        if not target.is_absolute():
            target = Path(__file__).resolve().parent / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        if server.poll() is None:
            server.terminate()
        server.wait(timeout=30)


if __name__ == "__main__":
    main()

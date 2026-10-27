"""Проверяет сохранность артефакта при пересоздании MLflow service."""
import os
import socket
import sys
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")
import mlflow
import psutil
from mlflow import MlflowClient
from src.common import connect, digest, load_params, save_json


def wait_healthy(uri, process, log_path):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"MLflow server exited; see {log_path}")
        try:
            with urllib.request.urlopen(f"{uri}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    raise TimeoutError(f"MLflow server did not become healthy; see {log_path}")


def start_local_server(port, backend_uri, artifact_root):
    command = [
        sys.executable, "-m", "mlflow", "server",
        "--backend-store-uri", backend_uri,
        "--default-artifact-root", artifact_root.as_uri(),
        "--serve-artifacts",
        "--host", "127.0.0.1",
        "--port", str(port),
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=Path.cwd(),
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    uri = f"http://127.0.0.1:{port}"
    try:
        wait_healthy(uri, process, "stderr redirected to DEVNULL")
    except Exception:
        stop_local_server(process)
        raise
    return process, uri


def stop_local_server(process):
    try:
        root = psutil.Process(process.pid)
        processes = root.children(recursive=True) + [root]
    except psutil.NoSuchProcess:
        processes = []
    for child in processes[:-1]:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.terminate()
    _, alive = psutil.wait_procs(processes, timeout=15)
    for child in alive:
        child.kill()
    psutil.wait_procs(alive, timeout=15)
    if process.poll() is None:
        process.wait(timeout=15)


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def local_restart_check(params, source, expected, tmp):
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", params["tracking"]["uri"])
    if not tracking_uri.startswith(("http://127.0.0.1", "http://localhost")):
        raise ValueError("Local restart check requires a loopback MLflow tracking URI")

    backend_uri = os.environ.get(
        "HW6_LOCAL_BACKEND_STORE_URI", "sqlite:///metrics/mlflow.db"
    )
    artifact_root = Path(os.environ.get("HW6_LOCAL_ARTIFACT_ROOT", "mlartifacts")).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    port = available_port()
    local_uri = f"http://127.0.0.1:{port}"
    process, _ = start_local_server(port, backend_uri, artifact_root)
    try:
        mlflow.set_tracking_uri(local_uri)
        mlflow.set_experiment("hw6-storage-check")
        with mlflow.start_run(run_name="storage-probe", tags={"kind": "check"}) as run:
            mlflow.log_artifact(str(source), "persistence")
            run_id = run.info.run_id
    finally:
        stop_local_server(process)

    process, _ = start_local_server(port, backend_uri, artifact_root)
    try:
        client = MlflowClient(tracking_uri=local_uri)
        dest = Path(tmp) / "download"
        dest.mkdir()
        restored = client.download_artifacts(run_id, "persistence/probe.txt", str(dest))
        if digest(restored) != expected:
            raise RuntimeError("Artifact changed after restarting the local MLflow server")
    finally:
        stop_local_server(process)

    return {
        "run_id": run_id,
        "sha256": expected,
        "mode": "local-server-restart",
        "verified_after_restart": True,
        "tracking_uri": local_uri,
    }


def main():
    params = load_params()
    with tempfile.TemporaryDirectory(prefix="hw6-storage-") as tmp:
        source = Path(tmp) / "probe.txt"
        source.write_text(str(uuid.uuid4()), encoding="utf-8")
        expected = digest(source)
        docker = __import__("shutil").which("docker")
        if docker:
            connect(params)
            with mlflow.start_run(run_name="storage-probe", tags={"kind": "check"}) as run:
                mlflow.log_artifact(str(source), "persistence")
                run_id = run.info.run_id
            project = os.environ.get("HW6_COMPOSE_PROJECT", "mlops-hw6")
            subprocess.run(
                [docker, "compose", "-p", project, "up", "-d", "--no-deps",
                 "--force-recreate", "--wait", "mlflow"],
                check=True,
            )
            dest = Path(tmp) / "download"
            dest.mkdir()
            restored = MlflowClient().download_artifacts(
                run_id, "persistence/probe.txt", str(dest)
            )
            if digest(restored) != expected:
                raise RuntimeError("Artifact changed after recreating the Docker container")
            proof = {
                "run_id": run_id,
                "sha256": expected,
                "mode": "docker-container-recreate",
                "verified_after_restart": True,
            }
        else:
            proof = local_restart_check(params, source, expected, tmp)

        proof.update(compose_sha256=digest("compose.yaml"))
        save_json("metrics/storage-check.json", proof)
        print(f"PASS: artifact persisted ({proof['mode']}), SHA256 matched")


if __name__ == "__main__":
    main()

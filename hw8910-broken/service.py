"""Local HTTP adapter for the release-aware inference service."""
import argparse
import json
import math
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MAX_NEW_TOKENS = 32
MAX_BODY_BYTES = 65536


class ServiceNotReady(RuntimeError):
    pass


class StubBackend:
    backend_name = "STUB ONLY"

    def __init__(self, manifest):
        self.loaded_id = manifest["model_id"]
        self.runtime_sha256 = manifest["runtime_sha256"]
        self.calls = 0
        self._lock = threading.Lock()

    def generate(self, prompt, limit):
        with self._lock:
            self.calls += 1
        time.sleep(0.01)
        return {"answer": f"STUB ONLY: {self.loaded_id}", "output_tokens": 1}


class ServiceState:
    def __init__(self, root, warmup_seconds, backend_factory=StubBackend):
        if (
            isinstance(warmup_seconds, bool)
            or not isinstance(warmup_seconds, (int, float))
            or not math.isfinite(warmup_seconds)
            or warmup_seconds < 0
        ):
            raise ValueError("warmup_seconds must be finite and non-negative")
        self.root = Path(root)
        self.manifest = json.loads((self.root / "accepted.json").read_text(encoding="utf-8"))
        self.backend = backend_factory(self.manifest)
        self.warmup_finished = threading.Event()

        def finish_warmup():
            time.sleep(warmup_seconds)
            self.warmup_finished.set()

        if warmup_seconds == 0:
            self.warmup_finished.set()
        else:
            threading.Thread(target=finish_warmup, daemon=True).start()

    @property
    def ready(self):
        return self.warmup_finished.is_set()

    def generate(self, prompt, max_new_tokens):
        if not self.ready:
            raise ServiceNotReady("backend is warming up")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if (
            type(max_new_tokens) is not int
            or not 1 <= max_new_tokens <= MAX_NEW_TOKENS
        ):
            raise ValueError(f"max_new_tokens must be an integer from 1 to {MAX_NEW_TOKENS}")

        started = time.perf_counter()
        result = self.backend.generate(prompt, max_new_tokens)
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            raise TypeError("backend.generate must return an object with a string answer")
        response = dict(result)
        response.update(
            model_id=self.manifest["model_id"],
            runtime_sha256=self.manifest["runtime_sha256"],
            request_id=str(uuid.uuid4()),
            generation_seconds=time.perf_counter() - started,
        )
        return response


def handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self.send_json(200, {"alive": True, "backend": "stub"})
            elif self.path == "/ready":
                self.send_json(200 if state.ready else 503, {"ready": state.ready})
            elif self.path == "/debug":
                self.send_json(
                    200,
                    {
                        "loaded_id": state.backend.loaded_id,
                        "calls": state.backend.calls,
                        "warmup_finished": state.warmup_finished.is_set(),
                    },
                )
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/generate":
                self.send_json(404, {"error": "not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json(400, {"error": "invalid Content-Length"})
                return
            if size < 0:
                self.send_json(400, {"error": "invalid Content-Length"})
                return
            if size > MAX_BODY_BYTES:
                self.send_json(413, {"error": "body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(size))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_json(400, {"error": "invalid JSON"})
                return
            if not isinstance(payload, dict):
                self.send_json(422, {"error": "request body must be an object"})
                return
            prompt = payload.get("prompt")
            limit = payload.get("max_new_tokens", 32)
            if not isinstance(prompt, str) or not prompt.strip():
                self.send_json(422, {"error": "prompt must be a non-empty string"})
                return
            if type(limit) is not int or not 1 <= limit <= MAX_NEW_TOKENS:
                self.send_json(
                    422,
                    {"error": f"max_new_tokens must be an integer from 1 to {MAX_NEW_TOKENS}"},
                )
                return
            try:
                self.send_json(200, state.generate(prompt, limit))
            except ServiceNotReady as exc:
                self.send_json(503, {"error": str(exc)})

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} - {fmt % args}", flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path(".lab"))
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--warmup-seconds", type=float, default=5)
    args = parser.parse_args()
    state = ServiceState(args.workspace, args.warmup_seconds)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(state))
    print(f"STUB ONLY: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

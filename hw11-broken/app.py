import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Annotated, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field, StrictStr


ROOT = Path(__file__).resolve().parent
ARTIFACT_PATH = Path(os.getenv("ARTIFACT_PATH", ROOT / "model-artifact.txt"))
HISTOGRAM_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 2, 5, 10)
LOGGER = logging.getLogger("hw11")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")


def _float_env(name: str, default: float, minimum: float = 0) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return value


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Annotated[StrictStr, Field(min_length=1, max_length=16)]
    content: Annotated[StrictStr, Field(max_length=8192)]


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: Annotated[StrictStr, Field(min_length=1, max_length=8192)]
    session_id: Annotated[StrictStr, Field(default="", max_length=128)]
    history: Annotated[list[Message], Field(default_factory=list, max_length=32)]
    prefix: Annotated[StrictStr, Field(default="", max_length=2048)]
    max_new_tokens: Annotated[int, Field(strict=True, ge=1, le=32)] = 8


class ServiceMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "inference_requests_total",
            "Requests by final outcome.",
            ("outcome",),
            registry=self.registry,
        )
        self.first_chunk = Histogram(
            "inference_ttfc_seconds",
            "Seconds from request admission to the first content chunk.",
            ("prefix_cache",),
            buckets=HISTOGRAM_BUCKETS,
            registry=self.registry,
        )
        self.full_response = Histogram(
            "inference_response_seconds",
            "Seconds from request arrival through the final content chunk.",
            ("prefix_cache",),
            buckets=HISTOGRAM_BUCKETS,
            registry=self.registry,
        )
        self.inter_chunk = Histogram(
            "inference_inter_chunk_seconds",
            "Seconds between consecutive content chunks in successful streams.",
            ("prefix_cache",),
            buckets=HISTOGRAM_BUCKETS,
            registry=self.registry,
        )
        self.cache_requests = Counter(
            "inference_prefix_cache_requests_total",
            "Requests by simulated local prefix-cache result.",
            ("result",),
            registry=self.registry,
        )
        self.queue_wait = Histogram(
            "inference_queue_wait_seconds",
            "Seconds spent waiting for an inference slot.",
            buckets=HISTOGRAM_BUCKETS,
            registry=self.registry,
        )
        self.running = Gauge(
            "inference_running",
            "Requests currently holding an inference slot.",
            registry=self.registry,
        )
        self.waiting = Gauge(
            "inference_waiting",
            "Requests waiting for an inference slot.",
            registry=self.registry,
        )


class Simulator:
    def __init__(self) -> None:
        self.cache_size = _int_env("PREFIX_CACHE_SIZE", 32, 0)
        self.cache_hit_seconds = _float_env("CACHE_HIT_SECONDS", 0.04)
        self.cache_miss_seconds = _float_env("CACHE_MISS_SECONDS", 0.16)
        self.chunk_seconds = _float_env("CHUNK_SECONDS", 0.025)
        self.cpu_burn_ms = _float_env("CPU_BURN_MS", 15)
        self.cache: OrderedDict[str, None] = OrderedDict()
        self.cache_lock = Lock()

    def lookup_prefix(self, prefix: str) -> bool:
        if not prefix or self.cache_size == 0:
            return False
        key = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        with self.cache_lock:
            if key not in self.cache:
                self.cache[key] = None
                if len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
                return False
            self.cache.move_to_end(key)
            return True

    def _burn_cpu(self) -> None:
        deadline = time.perf_counter() + self.cpu_burn_ms / 1000
        value = 0.5
        while time.perf_counter() < deadline:
            value = (value * 1.000001 + 0.25) % 97

    async def simulate_chunk(self, delay: float) -> None:
        await asyncio.to_thread(self._burn_cpu)
        if delay:
            await asyncio.sleep(delay)


def _sse(event: str, payload: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


def create_app() -> FastAPI:
    warmup_seconds = _float_env("WARMUP_SECONDS", 20)
    queue_timeout_seconds = _float_env("QUEUE_TIMEOUT_SECONDS", 2)
    max_concurrent = _int_env("MAX_CONCURRENT", 1)
    max_waiting = _int_env("MAX_WAITING", 8, 0)
    chunk_count = _int_env("CHUNK_COUNT", 6)
    model_version = os.getenv("MODEL_VERSION", "sim-v1")
    pod_name = os.getenv("POD_NAME", os.getenv("HOSTNAME", "local"))
    readiness_mode = os.getenv("READINESS_MODE", "backend")
    force_backend_unready = os.getenv("BACKEND_READY", "true").lower() == "false"
    if readiness_mode not in {"backend", "process"}:
        raise ValueError("READINESS_MODE must be 'backend' or 'process'")
    if not ARTIFACT_PATH.is_file():
        raise FileNotFoundError(f"simulator artifact not found: {ARTIFACT_PATH}")

    artifact_sha256 = hashlib.sha256(ARTIFACT_PATH.read_bytes()).hexdigest()
    expected_sha256 = os.getenv("ARTIFACT_SHA256", "")
    if expected_sha256 and artifact_sha256 != expected_sha256:
        raise ValueError("simulator artifact SHA-256 does not match ARTIFACT_SHA256")

    started_at = time.perf_counter()
    semaphore = asyncio.Semaphore(max_concurrent)
    waiting_requests = 0
    simulator = Simulator()
    metrics = ServiceMetrics()

    def warmup_finished() -> bool:
        return time.perf_counter() - started_at >= warmup_seconds

    def backend_ready() -> bool:
        return warmup_finished() and not force_backend_unready

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(title="HW11 simulated inference", lifespan=lifespan)
    app.state.metrics = metrics

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"alive": True, "pod": pod_name}

    @app.get("/startup")
    async def startup(response: Response) -> dict[str, object]:
        complete = warmup_finished()
        response.status_code = 200 if complete else 503
        return {"started": complete, "warmup_seconds": warmup_seconds}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, object]:
        available = readiness_mode == "process" or backend_ready()
        response.status_code = 200 if available else 503
        return {
            "ready": available,
            "backend_mode": "simulated",
            "model_version": model_version,
            "artifact_sha256": artifact_sha256,
        }

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/generate")
    async def generate(request: GenerateRequest, http_request: Request) -> StreamingResponse:
        nonlocal waiting_requests
        arrived_at = time.perf_counter()
        if not backend_ready():
            metrics.requests.labels(outcome="rejected").inc()
            raise HTTPException(status_code=503, detail="backend is not ready")

        supplied_request_id = http_request.headers.get("x-request-id", "")
        request_id = (
            supplied_request_id
            if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied_request_id)
            else str(uuid.uuid4())
        )
        if not request.prompt.strip():
            raise HTTPException(status_code=422, detail="prompt must be non-empty")
        session_id = request.session_id[:128]
        try:
            queued_at = time.perf_counter()
            if semaphore.locked():
                if waiting_requests >= max_waiting:
                    metrics.requests.labels(outcome="rejected").inc()
                    LOGGER.info(json.dumps({
                        "event": "request_rejected",
                        "request_id": request_id,
                        "session_id": session_id,
                        "pod": pod_name,
                        "version": model_version,
                        "reason": "queue_full",
                    }))
                    raise HTTPException(status_code=503, detail="inference queue is full")
                waiting_requests += 1
                metrics.waiting.inc()
                try:
                    if queue_timeout_seconds == 0:
                        raise TimeoutError
                    await asyncio.wait_for(semaphore.acquire(), timeout=queue_timeout_seconds)
                finally:
                    waiting_requests -= 1
                    metrics.waiting.dec()
            else:
                await semaphore.acquire()
            queue_seconds = time.perf_counter() - queued_at
        except TimeoutError:
            metrics.requests.labels(outcome="rejected").inc()
            LOGGER.info(json.dumps({
                "event": "request_rejected",
                "request_id": request_id,
                "session_id": session_id,
                "pod": pod_name,
                "version": model_version,
                "reason": "queue_timeout",
            }))
            raise HTTPException(status_code=503, detail="inference queue is full") from None

        metrics.queue_wait.observe(queue_seconds)
        metrics.running.inc()
        prefix_hit = simulator.lookup_prefix(request.prefix)
        prefix_cache_result = "hit" if prefix_hit else "miss"
        metrics.cache_requests.labels(result=prefix_cache_result).inc()
        context = [{"role": message.role, "content": message.content} for message in request.history]
        digest_source = json.dumps(
            {"prompt": request.prompt, "history": context},
            ensure_ascii=False,
            sort_keys=True,
        )
        context_digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]
        stream_finalized = False
        slot_released = False
        full_started_at = arrived_at
        first_chunk_seconds: float | None = None
        inter_chunk_seconds: list[float] = []

        def finish_stream(outcome: str) -> None:
            nonlocal stream_finalized
            if stream_finalized:
                return
            stream_finalized = True
            if outcome == "completed":
                metrics.requests.labels(outcome="completed").inc()
                if first_chunk_seconds is not None:
                    metrics.first_chunk.labels(prefix_cache=prefix_cache_result).observe(
                        first_chunk_seconds
                    )
                for interval in inter_chunk_seconds:
                    metrics.inter_chunk.labels(prefix_cache=prefix_cache_result).observe(interval)
                metrics.full_response.labels(prefix_cache=prefix_cache_result).observe(
                    time.perf_counter() - full_started_at
                )
            else:
                metrics.requests.labels(outcome="aborted").inc()
            LOGGER.info(json.dumps({
                "event": "request_finished" if outcome == "completed" else "request_interrupted",
                "request_id": request_id,
                "session_id": session_id,
                "pod": pod_name,
                "version": model_version,
                "outcome": outcome,
                "prefix_cache": prefix_cache_result,
            }))

        def release_slot() -> None:
            nonlocal slot_released
            if slot_released:
                return
            slot_released = True
            finish_stream("aborted")
            metrics.running.dec()
            semaphore.release()

        class ManagedStreamingResponse(StreamingResponse):
            async def __call__(self, scope, receive, send) -> None:
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    release_slot()

        async def stream() -> AsyncIterator[bytes]:
            nonlocal first_chunk_seconds
            previous_chunk_at: float | None = None
            try:
                yield _sse("meta", {
                    "request_id": request_id,
                    "session_id": session_id,
                    "pod": pod_name,
                    "model_version": model_version,
                    "artifact_sha256": artifact_sha256,
                    "backend_mode": "simulated",
                    "prefix_cache": "hit" if prefix_hit else "miss",
                    "queue_wait_seconds": queue_seconds,
                    "history_is_request_scoped": True,
                })
                for index in range(min(request.max_new_tokens, chunk_count)):
                    if index == 0:
                        delay = simulator.cache_hit_seconds if prefix_hit else simulator.cache_miss_seconds
                    else:
                        delay = simulator.chunk_seconds
                    await simulator.simulate_chunk(delay)
                    chunk_at = time.perf_counter()
                    if index == 0:
                        first_chunk_seconds = chunk_at - full_started_at
                    elif previous_chunk_at is not None:
                        inter_chunk_seconds.append(chunk_at - previous_chunk_at)
                    previous_chunk_at = chunk_at
                    yield _sse("chunk", {
                        "index": index,
                        "content": f"sim:{model_version}:{context_digest}:{index}",
                    })
                yield _sse("done", {
                    "request_id": request_id,
                    "output_chunks": min(request.max_new_tokens, chunk_count),
                    "backend_mode": "simulated",
                })
                finish_stream("completed")
            except asyncio.CancelledError:
                raise
            finally:
                release_slot()

        return ManagedStreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-ID": request_id,
            },
        )

    return app


app = create_app()

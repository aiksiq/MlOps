"""FastAPI application factory; the backend is injectable for fast tests."""
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field, StrictStr

from service import ServiceNotReady, ServiceState, StubBackend


TokenLimit = Annotated[int, Field(strict=True, ge=1, le=32)]


class GenerateRequest(BaseModel):
    prompt: StrictStr
    max_new_tokens: TokenLimit = 32


def create_app(
    root: Path = Path(".lab"),
    warmup_seconds: float = 5,
    backend_factory=StubBackend,
    state: ServiceState | None = None,
    enable_debug: bool = False,
) -> FastAPI:
    service_state = state if state is not None else ServiceState(root, warmup_seconds, backend_factory)
    app = FastAPI(title="Release-aware inference API")
    app.state.service = service_state

    @app.get("/health")
    def health():
        return {"alive": True, "backend": getattr(service_state.backend, "backend_name", "stub")}

    @app.get("/ready")
    def ready(response: Response):
        response.status_code = status.HTTP_200_OK if service_state.ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ready": service_state.ready}

    if enable_debug:
        @app.get("/debug")
        def debug():
            return {
                "loaded_id": service_state.backend.loaded_id,
                "calls": service_state.backend.calls,
                "warmup_finished": service_state.warmup_finished.is_set(),
            }

    @app.post("/generate")
    def generate(request: GenerateRequest):
        if not request.prompt.strip():
            raise HTTPException(status_code=422, detail="prompt must be a non-empty string")
        try:
            return service_state.generate(request.prompt, request.max_new_tokens)
        except ServiceNotReady as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return app

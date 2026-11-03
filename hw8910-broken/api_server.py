"""Run the FastAPI adapter against the accepted manifest."""
import argparse
import os
import sys
from pathlib import Path

import uvicorn

from api import create_app
from inference import ReleaseBackend
from service import StubBackend


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path(".lab"))
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--warmup-seconds", type=float, default=5)
    parser.add_argument("--backend", choices=("stub", "model"), default="stub")
    parser.add_argument("--hw67-root", type=Path, default=Path("../hw67-broken"))
    args = parser.parse_args()

    backend_factory = StubBackend
    if args.backend == "model":
        hw67_root = args.hw67_root.resolve()
        if not hw67_root.is_dir():
            raise FileNotFoundError(f"HW 6+7 project was not found: {hw67_root}")
        sys.path.insert(0, str(hw67_root))
        os.environ.setdefault("HW6_PARAMS", str(hw67_root / "params.yaml"))
        backend_factory = ReleaseBackend

    app = create_app(args.workspace, args.warmup_seconds, backend_factory)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

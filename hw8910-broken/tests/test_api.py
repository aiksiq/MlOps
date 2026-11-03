import asyncio
import tempfile
import unittest
from pathlib import Path
import sys

import httpx

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import lab
from api import create_app
from service import ServiceState


def _request(app, method, path, **kwargs):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


class FastAPIContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        lab.initialize(self.workspace)

    def test_strict_validation_precedes_backend_and_valid_call_uses_loaded_manifest(self):
        state = ServiceState(self.workspace, 0)
        app = create_app(state=state, enable_debug=True)
        for payload in (
            {"prompt": ""},
            {"prompt": "hello", "max_new_tokens": 0},
            {"prompt": "hello", "max_new_tokens": 100000},
            {"prompt": "hello", "max_new_tokens": "4"},
        ):
            with self.subTest(payload=payload):
                self.assertGreaterEqual(
                    _request(app, "POST", "/generate", json=payload).status_code,
                    400,
                )
                self.assertEqual(_request(app, "GET", "/debug").json()["calls"], 0)

        response = _request(
            app,
            "POST",
            "/generate",
            json={"prompt": "hello", "trace": "ignored"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model_id"], "fixture-A")
        self.assertEqual(_request(app, "GET", "/debug").json()["calls"], 1)

    def test_health_and_readiness_are_distinct(self):
        state = ServiceState(self.workspace, 1)
        app = create_app(state=state, enable_debug=True)
        self.assertEqual(_request(app, "GET", "/health").status_code, 200)
        self.assertEqual(_request(app, "GET", "/ready").status_code, 503)
        response = _request(app, "POST", "/generate", json={"prompt": "hello"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(_request(app, "GET", "/debug").json()["calls"], 0)

    def test_debug_is_disabled_by_default(self):
        state = ServiceState(self.workspace, 0)
        app = create_app(state=state)
        self.assertEqual(_request(app, "GET", "/debug").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)

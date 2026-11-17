import asyncio
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import GenerateRequest


def make_app(**settings):
    environment = {
        "ARTIFACT_PATH": str(ROOT / "model-artifact.txt"),
        "WARMUP_SECONDS": "0",
        "MAX_CONCURRENT": "1",
        "QUEUE_TIMEOUT_SECONDS": "0.01",
        "CHUNK_COUNT": "2",
        "CHUNK_SECONDS": "0",
        "CACHE_HIT_SECONDS": "0",
        "CACHE_MISS_SECONDS": "0",
        "CPU_BURN_MS": "0",
        "POD_NAME": "test-pod",
        "MODEL_VERSION": "test-v1",
    }
    environment.update({key: str(value) for key, value in settings.items()})
    with patch.dict(os.environ, environment, clear=False):
        from app import create_app

        return create_app()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = make_app()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def events(self, payload):
        response = await self.client.post("/generate", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        import json

        return [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]

    async def test_warmup_blocks_readiness_and_generation(self):
        app = make_app(WARMUP_SECONDS="0.15")
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            self.assertEqual((await client.get("/health")).status_code, 200)
            self.assertEqual((await client.get("/startup")).status_code, 503)
            self.assertEqual((await client.get("/ready")).status_code, 503)
            self.assertEqual(
                (await client.post("/generate", json={"prompt": "hello"})).status_code,
                503,
            )
            await asyncio.sleep(0.17)
            self.assertEqual((await client.get("/startup")).status_code, 200)
            self.assertEqual((await client.get("/ready")).status_code, 200)
        finally:
            await client.aclose()

    async def test_loaded_version_and_artifact_hash_are_returned(self):
        response = await self.client.get("/ready")
        self.assertEqual(response.json()["model_version"], "test-v1")
        self.assertEqual(
            response.json()["artifact_sha256"],
            hashlib.sha256((ROOT / "model-artifact.txt").read_bytes()).hexdigest(),
        )
        events = await self.events({"prompt": "release"})
        meta = events[0]
        self.assertEqual(meta["backend_mode"], "simulated")
        self.assertEqual(meta["pod"], "test-pod")

    async def test_two_dialogues_are_request_scoped_and_isolated(self):
        first = await self.events({
            "prompt": "first-control",
            "session_id": "session-a",
            "history": [{"role": "user", "content": "first-secret"}],
        })
        second = await self.events({
            "prompt": "second-control",
            "session_id": "session-b",
            "history": [{"role": "user", "content": "second-secret"}],
        })
        self.assertNotEqual(first[1]["content"], second[1]["content"])
        self.assertNotIn("first-secret", str(second))
        self.assertNotIn("second-secret", str(first))

    async def test_simulator_counts_completed_requests_and_exposes_queue_metrics(self):
        await self.events({"prompt": "one"})
        await self.events({"prompt": "two"})
        metrics = (await self.client.get("/metrics")).text
        self.assertIn('inference_requests_total{outcome="completed"} 2.0', metrics)
        self.assertIn('inference_ttfc_seconds_count{prefix_cache="miss"} 2.0', metrics)
        self.assertIn('inference_inter_chunk_seconds_count{prefix_cache="miss"} 2.0', metrics)
        self.assertIn("inference_running 0.0", metrics)
        self.assertIn("inference_waiting 0.0", metrics)

    async def test_prefix_cache_cost_is_paid_once_and_hits_are_counted(self):
        first = await self.events({"prompt": "same-prefix-a", "prefix": "shared-prefix"})
        second = await self.events({"prompt": "same-prefix-b", "prefix": "shared-prefix"})
        self.assertEqual(first[0]["prefix_cache"], "miss")
        self.assertEqual(second[0]["prefix_cache"], "hit")
        metrics = (await self.client.get("/metrics")).text
        self.assertIn('inference_prefix_cache_requests_total{result="miss"} 1.0', metrics)
        self.assertIn('inference_prefix_cache_requests_total{result="hit"} 1.0', metrics)
        self.assertIn('inference_inter_chunk_seconds_count{prefix_cache="miss"} 1.0', metrics)
        self.assertIn('inference_inter_chunk_seconds_count{prefix_cache="hit"} 1.0', metrics)

    async def test_queue_overflow_is_a_counted_rejection(self):
        app = make_app(CHUNK_COUNT="2", CHUNK_SECONDS="0.2", QUEUE_TIMEOUT_SECONDS="0.01")
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            first = asyncio.create_task(client.post("/generate", json={"prompt": "slow"}))
            await asyncio.sleep(0.01)
            rejected = await client.post("/generate", json={"prompt": "overload"})
            self.assertEqual(rejected.status_code, 503)
            await first
            metrics = (await client.get("/metrics")).text
            self.assertIn('inference_requests_total{outcome="rejected"} 1.0', metrics)
        finally:
            await client.aclose()

    async def test_bounded_queue_rejects_requests_beyond_capacity(self):
        app = make_app(
            CHUNK_SECONDS="0.2",
            CACHE_MISS_SECONDS="0",
            MAX_WAITING="1",
            QUEUE_TIMEOUT_SECONDS="1",
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            running = asyncio.create_task(client.post("/generate", json={"prompt": "running"}))
            await asyncio.sleep(0.01)
            waiting = asyncio.create_task(client.post("/generate", json={"prompt": "waiting"}))
            await asyncio.sleep(0.01)
            rejected = await client.post("/generate", json={"prompt": "queue-full"})
            self.assertEqual(rejected.status_code, 503)
            self.assertIn("queue is full", rejected.text)
            await running
            self.assertEqual((await waiting).status_code, 200)
            metrics = (await client.get("/metrics")).text
            self.assertIn('inference_requests_total{outcome="rejected"} 1.0', metrics)
            self.assertIn("inference_waiting 0.0", metrics)
        finally:
            await client.aclose()

    async def test_cancelled_stream_is_counted_and_releases_capacity(self):
        app = make_app(CHUNK_SECONDS="0.2")
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/generate")
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/generate",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        })
        response = await endpoint(
            GenerateRequest(prompt="disconnect"),
            request,
        )
        stream = response.body_iterator
        await anext(stream)
        await stream.aclose()

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )
        try:
            metrics = (await client.get("/metrics")).text
            self.assertIn('inference_requests_total{outcome="aborted"} 1.0', metrics)
            self.assertIn("inference_running 0.0", metrics)
            self.assertIn("inference_waiting 0.0", metrics)
        finally:
            await client.aclose()

    async def test_unstarted_response_releases_reserved_slot(self):
        app = make_app()
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/generate")
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/generate",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        })
        response = await endpoint(GenerateRequest(prompt="disconnect before stream"), request)

        async def fail_before_body(message):
            if message["type"] == "http.response.start":
                raise ConnectionError("client disconnected before stream start")

        with self.assertRaisesRegex(ConnectionError, "disconnected"):
            await response(
                request.scope,
                lambda: asyncio.sleep(0, result={"type": "http.request", "body": b"", "more_body": False}),
                fail_before_body,
            )

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )
        try:
            metrics = (await client.get("/metrics")).text
            self.assertIn('inference_requests_total{outcome="aborted"} 1.0', metrics)
            self.assertIn("inference_running 0.0", metrics)
        finally:
            await client.aclose()

    async def test_artifact_hash_mismatch_fails_startup(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            make_app(ARTIFACT_SHA256="0" * 64)


if __name__ == "__main__":
    unittest.main()

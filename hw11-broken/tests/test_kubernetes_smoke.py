import asyncio
import json
import os
import unittest
import uuid

import httpx


class KubernetesSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_warming_pod_is_not_ready_or_serving(self):
        url = os.getenv("HW11_WARMING_URL")
        if not url:
            self.skipTest("set HW11_WARMING_URL to a port-forward for a newly created Pod")
        async with httpx.AsyncClient(base_url=url, timeout=3) as client:
            ready = await client.get("/ready")
            self.assertEqual(ready.status_code, 503)
            generate = await client.post("/generate", json={"prompt": "must stay out of service"})
            self.assertEqual(generate.status_code, 503)

    async def test_rollback_restores_expected_model_version(self):
        url = os.getenv("HW11_SERVICE_URL")
        expected = os.getenv("HW11_EXPECTED_MODEL_VERSION")
        if not url or not expected:
            self.skipTest("set HW11_SERVICE_URL and HW11_EXPECTED_MODEL_VERSION after helm rollback")
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            ready = await client.get("/ready")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["model_version"], expected)
            meta = await self._generate(client)
            self.assertEqual(meta["model_version"], expected)
            self.assertEqual(meta["backend_mode"], "simulated")

    async def test_replacement_pod_recovers_artifact_and_dialogue(self):
        url = os.getenv("HW11_REPLACEMENT_URL")
        expected_version = os.getenv("HW11_EXPECTED_MODEL_VERSION")
        expected_sha = os.getenv("HW11_EXPECTED_ARTIFACT_SHA256")
        old_pod = os.getenv("HW11_PRE_REPLACE_POD")
        if not all((url, expected_version, expected_sha, old_pod)):
            self.skipTest(
                "set the replacement Pod URL, expected version/hash, and previous Pod name"
            )
        deadline = asyncio.get_running_loop().time() + 90
        last_meta = None
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            while asyncio.get_running_loop().time() < deadline:
                ready = await client.get("/ready")
                if ready.status_code == 200 and ready.json()["model_version"] == expected_version:
                    last_meta = await self._generate(client)
                    if last_meta["pod"] != old_pod:
                        break
                await asyncio.sleep(1)
        self.assertIsNotNone(last_meta, "service did not return a response from a replacement Pod")
        self.assertNotEqual(last_meta["pod"], old_pod)
        self.assertEqual(last_meta["model_version"], expected_version)
        self.assertEqual(last_meta["artifact_sha256"], expected_sha)
        self.assertTrue(last_meta["history_is_request_scoped"])

    @staticmethod
    async def _generate(client: httpx.AsyncClient) -> dict[str, object]:
        response = await client.post(
            "/generate",
            headers={"X-Request-ID": str(uuid.uuid4())},
            json={
                "prompt": "continue this conversation",
                "session_id": "replacement-check",
                "history": [{"role": "user", "content": "persisted control value"}],
            },
        )
        if response.status_code != 200:
            raise AssertionError(f"generation failed with HTTP {response.status_code}")
        lines = response.text.splitlines()
        for index, line in enumerate(lines):
            if line == "event: meta" and index + 1 < len(lines):
                return json.loads(lines[index + 1][6:])
        raise AssertionError("stream did not return a meta event")


if __name__ == "__main__":
    unittest.main()

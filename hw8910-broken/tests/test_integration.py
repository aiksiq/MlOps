import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import lab
import release


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    return response.status, json.loads(response.read().decode("utf-8"))


class HomeworkProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.services = []
        self.addCleanup(self.temp.cleanup)
        self._lab("init")

    def _lab(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(PROJECT / "lab.py"),
                "--workspace",
                str(self.workspace),
                *args,
            ],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=20,
        )

    def _record_for_test(self, manifest, report):
        if hasattr(lab, "_record_publication"):
            return lab._record_publication(self.workspace, manifest, report)
        with (self.workspace / "publications.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "model_id": manifest["model_id"],
                "evaluation_run_id": report["evaluation_run_id"],
            }) + "\n")
        return True

    def test_subprocess_recovery_reuses_content_cache_and_invalidates_inputs(self):
        failed = self._lab("run", "--fail-after-prepare")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("PREPARED", failed.stdout)

        recovered = self._lab("run")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("CACHE_HIT", recovered.stdout)

        input_path = self.workspace / "input.txt"
        input_path.write_text("changed bytes\n", encoding="utf-8")
        changed_data = self._lab("run")
        self.assertEqual(changed_data.returncode, 0, changed_data.stderr)
        self.assertIn("PREPARED", changed_data.stdout)

        config_path = self.workspace / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_length"] += 1
        config_path.write_text(json.dumps(config), encoding="utf-8")
        changed_config = self._lab("run")
        self.assertEqual(changed_config.returncode, 0, changed_config.stderr)
        self.assertIn("PREPARED", changed_config.stdout)

    def test_subprocess_repeated_publication_is_idempotent_after_restart(self):
        first = self._lab("run")
        second = self._lab("run")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("PUBLISHED fixture-B", first.stdout)
        self.assertIn("ALREADY_PUBLISHED fixture-B", second.stdout)
        events = [
            json.loads(line)
            for line in (self.workspace / "publications.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["model_id"], "fixture-B")

    def test_new_process_repairs_journal_after_pointer_was_replaced(self):
        publish = release.publish

        def publish_then_fail(manifest, report, target):
            publish(manifest, report, target)
            raise OSError("injected crash after pointer replacement")

        with patch.object(release, "publish", side_effect=publish_then_fail):
            with self.assertRaises(OSError):
                lab.run(self.workspace, "report-B.json")
        self.assertEqual(
            json.loads((self.workspace / "accepted.json").read_text(encoding="utf-8"))["model_id"],
            "fixture-B",
        )
        self.assertFalse((self.workspace / "publications.jsonl").exists())

        retried = self._lab("run")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        retried_again = self._lab("run")
        self.assertEqual(retried_again.returncode, 0, retried_again.stderr)
        events = (self.workspace / "publications.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(events), 1)

    def test_init_refuses_overwrite_existing_workspace(self):
        before = (self.workspace / "accepted.json").read_bytes()
        result = self._lab("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.workspace / "accepted.json").read_bytes(), before)

    def test_incomplete_rejected_and_foreign_reports_preserve_accepted_pointer(self):
        before = (self.workspace / "accepted.json").read_bytes()
        for report_name in (
            "report-incomplete.json",
            "report-reject.json",
            "report-A.json",
        ):
            with self.subTest(report=report_name):
                result = self._lab("run", "--report", report_name)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    (self.workspace / "accepted.json").read_bytes(),
                    before,
                )

    def _start_service(self, warmup_seconds):
        port = _free_port()
        process = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT / "service.py"),
                "--workspace",
                str(self.workspace),
                "--port",
                str(port),
                "--warmup-seconds",
                str(warmup_seconds),
            ],
            cwd=PROJECT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.services.append(process)
        self.addCleanup(self._stop_service, process)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read()
                self.fail(f"Service exited before startup: {output}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2) as response:
                    if response.status == 200:
                        return process, f"http://127.0.0.1:{port}"
            except (OSError, URLError):
                time.sleep(0.05)
        self.fail("Service did not become available")

    @staticmethod
    def _stop_service(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()

    def test_http_health_ready_and_generation_wait_for_warmup(self):
        process, base = self._start_service(1.5)
        self.assertEqual(_request(base + "/health")[0], 200)
        self.assertEqual(_request(base + "/ready")[0], 503)
        self.assertEqual(_request(base + "/generate", {"prompt": "hello"})[0], 503)
        self.assertEqual(_request(base + "/debug")[1]["calls"], 0)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _request(base + "/ready")[0] != 200:
            time.sleep(0.05)
        status, response = _request(base + "/generate", {"prompt": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(response["model_id"], "fixture-A")
        self.assertEqual(_request(base + "/debug")[1]["calls"], 1)
        self._stop_service(process)

    def test_http_invalid_requests_never_call_backend(self):
        process, base = self._start_service(0)
        for payload in (
            {"prompt": ""},
            {"prompt": "hello", "max_new_tokens": 0},
            {"prompt": "hello", "max_new_tokens": 100000},
            {"prompt": "hello", "max_new_tokens": "4"},
        ):
            with self.subTest(payload=payload):
                self.assertGreaterEqual(_request(base + "/generate", payload)[0], 400)
                self.assertEqual(_request(base + "/debug")[1]["calls"], 0)

        status, response = _request(base + "/generate", {"prompt": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(response["output_tokens"], 1)
        self.assertEqual(_request(base + "/debug")[1]["calls"], 1)

    def test_http_response_reports_loaded_version_until_managed_restart(self):
        process, base = self._start_service(0)
        self.assertEqual(
            _request(base + "/generate", {"prompt": "hello"})[1]["model_id"],
            "fixture-A",
        )
        updated = self._lab("run")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        response = _request(base + "/generate", {"prompt": "hello"})[1]
        self.assertEqual(response["model_id"], "fixture-A")
        self.assertEqual(_request(base + "/debug")[1]["loaded_id"], "fixture-A")

        self._stop_service(process)
        process_b, restarted = self._start_service(0)
        response = _request(restarted + "/generate", {"prompt": "hello"})[1]
        self.assertEqual(response["model_id"], "fixture-B")
        self.assertEqual(_request(restarted + "/debug")[1]["loaded_id"], "fixture-B")

        manifest_a = lab.read(self.workspace / "manifest-A.json")
        report_a = lab.read(self.workspace / "report-A.json")
        release.publish(manifest_a, report_a, self.workspace / "accepted.json")
        self._record_for_test(manifest_a, report_a)
        self._stop_service(process_b)
        process_a, restarted = self._start_service(0)
        response = _request(restarted + "/generate", {"prompt": "hello"})[1]
        self.assertEqual(response["model_id"], "fixture-A")
        self.assertEqual(_request(restarted + "/debug")[1]["loaded_id"], "fixture-A")
        if hasattr(lab, "_record_publication"):
            self.assertFalse(self._record_for_test(manifest_a, report_a))
        else:
            self._record_for_test(manifest_a, report_a)
        events = [
            json.loads(line)
            for line in (self.workspace / "publications.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["model_id"], "fixture-A")
        self._stop_service(process_a)


if __name__ == "__main__":
    unittest.main(verbosity=2)

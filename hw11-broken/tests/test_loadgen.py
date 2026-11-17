import json
import contextlib
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from loadgen import FIELDS, _percentile, _write_results


class LoadgenTests(unittest.TestCase):
    def test_percentile_interpolates_empirical_observations(self):
        self.assertEqual(_percentile([1.0, 3.0], 0.5), 2.0)
        self.assertEqual(_percentile([1.0, 3.0], 0.95), 2.9)
        self.assertIsNone(_percentile([], 0.95))

    def test_summary_separates_successes_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "requests.csv"
            base = {field: "" for field in FIELDS}
            successful = [
                {
                    **base,
                    "sent_at_utc": "sent",
                    "outcome": "completed",
                    "sent_offset_seconds": sent,
                    "first_chunk_offset_seconds": first,
                    "completed_offset_seconds": full,
                    "queue_wait_seconds": queue,
                    "inter_chunk_latencies_seconds": intervals,
                }
                for sent, first, full, queue, intervals in (
                    ("0", "0.2", "0.5", "0.01", "[0.1,0.2]"),
                    ("1", "1.4", "1.9", "0.03", "[0.2]"),
                )
            ]
            rejected = {**base, "sent_at_utc": "sent", "outcome": "rejected"}
            args = SimpleNamespace(
                run_id="summary-test",
                scenario="balanced",
                duration=2,
                total_rps=1.5,
                output=output,
                stdout_csv=True,
            )
            console = io.StringIO()
            with contextlib.redirect_stdout(console):
                _write_results(args, [*successful, rejected])
            summary = json.loads(output.with_suffix(".summary.json").read_text(encoding="utf-8"))
            captured = console.getvalue()
            self.assertIn("HW11_CSV_BEGIN\n", captured)
            self.assertIn("run_id,scenario,policy,client_id,sequence", captured)
            self.assertIn("HW11_CSV_END", captured)
            self.assertIn("summary-test", captured)
            self.assertEqual(summary["counts"]["planned"], 3)
            self.assertEqual(summary["counts"]["sent"], 3)
            self.assertEqual(summary["counts"]["completed"], 2)
            self.assertEqual(summary["counts"]["rejected"], 1)
            self.assertEqual(summary["policy"], "normal")
            self.assertEqual(summary["successful_latency_seconds"]["observations"], 2)
            self.assertAlmostEqual(summary["successful_latency_seconds"]["ttfc_p50"], 0.3)
            self.assertEqual(summary["successful_latency_seconds"]["queue_observations"], 2)
            with output.open(encoding="utf-8", newline="") as file:
                written = list(csv.DictReader(file))
            self.assertTrue(all(row["scenario"] == "balanced" for row in written))
            self.assertTrue(all(row["policy"] == "normal" for row in written))


if __name__ == "__main__":
    unittest.main()

import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plot_results import (
    plot_hpa_load,
    plot_pod_latency,
    plot_policy_comparison,
    plot_warmup,
    read_requests,
)


class PlotTests(unittest.TestCase):
    def test_four_plots_render_from_explicit_input_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for scenario in ("balanced", "hot"):
                for policy in ("normal", "affinity"):
                    for run in range(1, 4):
                        run_id = f"{scenario}{'-affinity' if policy == 'affinity' else ''}-{run:02d}"
                        for pod_index in range(2):
                            rows.append({
                                "run_id": run_id,
                                "scenario": scenario,
                                "policy": policy,
                                "client_id": f"client-{pod_index}",
                                "sent_at_utc": datetime.fromtimestamp(
                                    1_800_000_000 + run * 10 + pod_index, timezone.utc
                                ).isoformat(),
                                "sent_offset_seconds": str(pod_index),
                                "first_chunk_offset_seconds": str(pod_index + 0.1 + run / 100),
                                "completed_offset_seconds": str(pod_index + 0.4 + run / 100),
                                "outcome": "completed",
                                "request_id": f"{run_id}-{pod_index}",
                                "session_id": f"session-{pod_index}",
                                "pod": f"simulator-{pod_index}",
                                "model_version": "sim-v1",
                                "backend_mode": "simulated",
                                "prefix_cache": "miss",
                                "queue_wait_seconds": "0.01",
                                "output_chunks": "6",
                                "inter_chunk_latencies_seconds": "[0.04,0.04]",
                            })
            requests_path = root / "requests.csv"
            with requests_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)

            samples = []
            for second, ready_count, replicas in ((0, 0, 2), (10, 2, 2), (20, 3, 3), (30, 2, 2)):
                pods = [
                    {"name": f"simulator-{index}", "ready": index < ready_count}
                    for index in range(3)
                ]
                endpoints = [{"pod": pod["name"], "ready": pod["ready"]} for pod in pods]
                samples.append({
                    "timestamp": datetime.fromtimestamp(
                        1_800_000_000 + second, timezone.utc
                    ).isoformat(),
                    "pods": pods,
                    "endpoints": endpoints,
                    "hpa": {"currentReplicas": replicas, "desiredReplicas": replicas},
                })
            timeline_path = root / "timeline.jsonl"
            timeline_path.write_text(
                "\n".join(json.dumps(sample) for sample in samples) + "\n",
                encoding="utf-8",
            )
            timeline = [json.loads(line) for line in timeline_path.read_text(encoding="utf-8").splitlines()]
            requests = read_requests([requests_path])
            output = root / "plots"

            plot_warmup(timeline, output / "01.svg")
            plot_pod_latency(requests, output / "02.svg")
            plot_policy_comparison(requests, output / "03.svg")
            plot_hpa_load(timeline, requests, output / "04.svg")

            for number in range(1, 5):
                chart = (output / f"0{number}.svg").read_text(encoding="utf-8")
                self.assertIn("<svg", chart)
                self.assertIn("</svg>", chart)


if __name__ == "__main__":
    unittest.main()

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape


COLORS = ("#2563eb", "#dc2626", "#059669", "#9333ea", "#d97706", "#0891b2")
WIDTH = 1080


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile without observations")
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def read_requests(paths: list[Path]) -> list[dict[str, str]]:
    rows = []
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as file:
            rows.extend(csv.DictReader(file))
    if not rows:
        raise ValueError("no request rows were found")
    if any(row.get("backend_mode") != "simulated" for row in rows):
        raise ValueError("these plots accept only explicitly marked simulated-backend data")
    return rows


def _draw_panel(
    series: list[tuple[str, list[tuple[float, float]]]],
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    y_label: str,
    top: int,
    height: int,
    x_ticks: list[tuple[float, str]],
) -> list[str]:
    left, right = 90, 820
    bottom = top + height - 46
    plot_top = top + 18
    plot_height = bottom - plot_top
    plot_width = right - left
    output = [
        f'<text x="20" y="{top + 20}" font-size="15" fill="#334155">{escape(y_label)}</text>',
        f'<line x1="{left}" y1="{plot_top}" x2="{left}" y2="{bottom}" stroke="#64748b"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#64748b"/>',
    ]
    for tick in range(5):
        value = y_min + (y_max - y_min) * tick / 4
        y = bottom - plot_height * tick / 4
        output.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        output.append(f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="#64748b">{value:.3g}</text>')
    for position, label in x_ticks:
        x = left + (position - x_min) / max(x_max - x_min, 1e-9) * plot_width
        output.append(f'<line x1="{x:.1f}" y1="{bottom}" x2="{x:.1f}" y2="{bottom + 4}" stroke="#64748b"/>')
        output.append(f'<text x="{x:.1f}" y="{bottom + 20}" text-anchor="middle" font-size="11" fill="#64748b">{escape(label)}</text>')
    for index, (name, points) in enumerate(series):
        color = COLORS[index % len(COLORS)]
        valid = [(x, y) for x, y in points if math.isfinite(x) and math.isfinite(y)]
        coords = [
            (
                left + (x - x_min) / max(x_max - x_min, 1e-9) * plot_width,
                bottom - (y - y_min) / max(y_max - y_min, 1e-9) * plot_height,
            )
            for x, y in valid
        ]
        if coords:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            output.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>')
            for x, y in coords:
                output.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')
        legend_y = top + 24 + index * 19
        output.append(f'<line x1="850" y1="{legend_y - 4}" x2="872" y2="{legend_y - 4}" stroke="{color}" stroke-width="3"/>')
        output.append(f'<text x="880" y="{legend_y}" font-size="12" fill="#334155">{escape(name)}</text>')
    return output


def line_chart(
    title: str,
    x_label: str,
    y_label: str,
    series: list[tuple[str, list[tuple[float, float]]]],
    x_ticks: list[tuple[float, str]],
    output: Path,
) -> None:
    values = [y for _, points in series for _, y in points if math.isfinite(y)]
    x_values = [x for _, points in series for x, _ in points if math.isfinite(x)]
    if not values or not x_values:
        raise ValueError(f"{title}: no finite values to plot")
    x_min, x_max = min(x_values), max(x_values)
    y_min = min(0.0, min(values))
    y_max = max(values) * 1.1 if max(values) > 0 else 1.0
    if y_max <= y_min:
        y_max = y_min + 1
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="500" viewBox="0 0 {WIDTH} 500">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{WIDTH / 2}" y="28" text-anchor="middle" font-size="20" font-weight="bold" fill="#0f172a">{escape(title)}</text>',
    ]
    body.extend(_draw_panel(series, x_min, x_max, y_min, y_max, y_label, 42, 390, x_ticks))
    body.append(f'<text x="455" y="476" text-anchor="middle" font-size="13" fill="#334155">{escape(x_label)}</text>')
    body.append("</svg>")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(body), encoding="utf-8")


def paired_line_chart(
    title: str,
    x_label: str,
    top_label: str,
    top_series: list[tuple[str, list[tuple[float, float]]]],
    bottom_label: str,
    bottom_series: list[tuple[str, list[tuple[float, float]]]],
    x_ticks: list[tuple[float, str]],
    output: Path,
) -> None:
    all_x = [x for _, points in [*top_series, *bottom_series] for x, _ in points]
    if not all_x:
        raise ValueError(f"{title}: no finite values to plot")
    x_min, x_max = min(all_x), max(all_x)
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="580" viewBox="0 0 {WIDTH} 580">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{WIDTH / 2}" y="28" text-anchor="middle" font-size="20" font-weight="bold" fill="#0f172a">{escape(title)}</text>',
    ]
    for label, series, top, height in (
        (top_label, top_series, 42, 220),
        (bottom_label, bottom_series, 282, 220),
    ):
        values = [y for _, points in series for _, y in points if math.isfinite(y)]
        if not values:
            raise ValueError(f"{title}: {label} has no finite observations")
        y_min = min(0.0, min(values))
        y_max = max(values) * 1.1 if max(values) > 0 else 1.0
        if y_max <= y_min:
            y_max = y_min + 1
        body.extend(_draw_panel(series, x_min, x_max, y_min, y_max, label, top, height, x_ticks))
    body.append(f'<text x="455" y="564" text-anchor="middle" font-size="13" fill="#334155">{escape(x_label)}</text>')
    body.append("</svg>")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(body), encoding="utf-8")


def _read_timeline(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8-sig") as file:
        samples = [json.loads(line) for line in file if line.strip()]
    if len(samples) < 2:
        raise ValueError(f"{path}: timeline needs at least two samples")
    return samples


def plot_warmup(timeline: list[dict[str, object]], output: Path) -> None:
    start = datetime.fromisoformat(str(timeline[0]["timestamp"])).timestamp()
    ready, endpoints = [], []
    for sample in timeline:
        elapsed = datetime.fromisoformat(str(sample["timestamp"])).timestamp() - start
        pods = sample.get("pods", [])
        slices = sample.get("endpoints", [])
        ready.append((elapsed, float(sum(bool(pod["ready"]) for pod in pods))))
        endpoints.append((elapsed, float(sum(endpoint.get("ready") is True for endpoint in slices))))
    ticks = [(ready[0][0], "0 s"), (ready[-1][0], f"{ready[-1][0]:.0f} s")]
    line_chart(
        "Warmup and Service readiness",
        "Time from first sample",
        "Ready Pods / eligible Service endpoints (count)",
        [("Ready Pods", ready), ("Ready Service endpoints", endpoints)],
        ticks,
        output,
    )


def plot_pod_latency(rows: list[dict[str, str]], output: Path) -> None:
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"ttfc": [], "queue": []})
    for row in rows:
        pod = row.get("pod", "")
        if row.get("outcome") != "completed" or not pod:
            continue
        if row.get("first_chunk_offset_seconds") and row.get("sent_offset_seconds"):
            grouped[pod]["ttfc"].append(
                float(row["first_chunk_offset_seconds"]) - float(row["sent_offset_seconds"])
            )
        if row.get("queue_wait_seconds"):
            grouped[pod]["queue"].append(float(row["queue_wait_seconds"]))
    pods = sorted(grouped)
    if not pods or any(not grouped[pod]["ttfc"] or not grouped[pod]["queue"] for pod in pods):
        raise ValueError("Pod latency chart needs successful requests with TTFC and queue observations")
    x = [(float(index), pod) for index, pod in enumerate(pods)]
    line_chart(
        "TTFC and queue p95 by Pod",
        "Pod",
        "Seconds (p95)",
        [
            ("TTFC p95", [(float(i), percentile(grouped[pod]["ttfc"], 0.95)) for i, pod in enumerate(pods)]),
            ("Queue p95", [(float(i), percentile(grouped[pod]["queue"], 0.95)) for i, pod in enumerate(pods)]),
        ],
        x,
        output,
    )


def plot_policy_comparison(rows: list[dict[str, str]], output: Path) -> None:
    runs: dict[str, list[float]] = defaultdict(list)
    run_scenario: dict[str, str] = {}
    run_policy: dict[str, str] = {}
    for row in rows:
        run_id = row.get("run_id", "")
        run_scenario[run_id] = row.get("scenario") or (
            "hot" if "hot" in run_id else "balanced"
        )
        run_policy[run_id] = row.get("policy") or (
            "affinity" if "affinity" in run_id else "normal"
        )
        if row.get("outcome") == "completed" and row.get("sent_offset_seconds") and row.get("first_chunk_offset_seconds"):
            runs[run_id].append(
                float(row["first_chunk_offset_seconds"]) - float(row["sent_offset_seconds"])
            )
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for run_id, values in runs.items():
        if values:
            grouped[(run_scenario[run_id], run_policy[run_id])].append(percentile(values, 0.95))
    expected = [(scenario, policy) for scenario in ("balanced", "hot") for policy in ("normal", "affinity")]
    incomplete = [f"{scenario}/{policy}" for scenario, policy in expected if len(grouped[(scenario, policy)]) < 3]
    if incomplete:
        raise ValueError("policy chart requires three completed runs per combination; missing: " + ", ".join(incomplete))
    categories = ("balanced", "hot")
    series = []
    for policy in ("normal", "affinity"):
        series.append((
            policy,
            [
                (float(index), statistics.mean(grouped[(scenario, policy)]))
                for index, scenario in enumerate(categories)
            ],
        ))
    line_chart(
        "Policy comparison — mean run p95 TTFC",
        "Traffic scenario",
        "Seconds",
        series,
        [(0.0, "balanced"), (1.0, "hot")],
        output,
    )


def plot_hpa_load(
    timeline: list[dict[str, object]],
    rows: list[dict[str, str]],
    output: Path,
) -> None:
    start = datetime.fromisoformat(str(timeline[0]["timestamp"])).timestamp()
    current, desired = [], []
    for sample in timeline:
        hpa = sample.get("hpa", {})
        elapsed = datetime.fromisoformat(str(sample["timestamp"])).timestamp() - start
        current.append((elapsed, float(hpa.get("currentReplicas") or 0)))
        desired.append((elapsed, float(hpa.get("desiredReplicas") or 0)))
    if current[0][1] != 2 or max(value for _, value in current) < 3 or current[-1][1] != 2:
        raise ValueError("HPA chart needs an observed 2-to-3-to-2 replica transition")

    sent: dict[int, int] = defaultdict(int)
    for row in rows:
        stamp = row.get("sent_at_utc", "")
        if stamp:
            elapsed = datetime.fromisoformat(stamp).timestamp() - start
            sent[math.floor(elapsed / 5)] += 1
    rate = [(index * 5 + 2.5, count / 5) for index, count in sorted(sent.items())]
    series = [("Current replicas", current), ("Desired replicas", desired)]
    x_values = [x for _, points in [*series, ("Sent requests/s", rate)] for x, _ in points]
    x_min, x_max = min(x_values), max(x_values)
    ticks = [(x_min, f"{x_min:.0f} s"), (x_max, f"{x_max:.0f} s")]
    paired_line_chart(
        "HPA scaling and offered load",
        "Time from first HPA sample",
        "Replicas",
        series,
        "Requests/s",
        [("Sent requests/s", rate)],
        ticks,
        output,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build four SVG plots from real HW11 timeline and request CSVs.")
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--warmup-timeline", type=Path, required=True)
    parser.add_argument("--requests", type=Path, nargs="+", required=True)
    parser.add_argument("--hpa-request", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("results/plots"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    warmup_timeline = _read_timeline(args.warmup_timeline)
    hpa_timeline = _read_timeline(args.timeline)
    rows = read_requests(args.requests)
    hpa_rows = read_requests([args.hpa_request])
    plot_warmup(warmup_timeline, args.output / "01-warmup-ready.svg")
    plot_pod_latency(rows, args.output / "02-pod-latency-queue.svg")
    plot_policy_comparison(rows, args.output / "03-policy-comparison.svg")
    plot_hpa_load(hpa_timeline, hpa_rows, args.output / "04-hpa-load.svg")
    print(f"Created four SVG plots in {args.output}")


if __name__ == "__main__":
    main()

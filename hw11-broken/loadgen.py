import argparse
import asyncio
import csv
import hashlib
import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


FIELDS = (
    "run_id",
    "scenario",
    "policy",
    "client_id",
    "sequence",
    "planned_at_utc",
    "planned_offset_seconds",
    "sent_at_utc",
    "sent_offset_seconds",
    "first_chunk_offset_seconds",
    "completed_offset_seconds",
    "status_code",
    "outcome",
    "request_id",
    "session_id",
    "pod",
    "model_version",
    "backend_mode",
    "prefix_cache",
    "queue_wait_seconds",
    "output_chunks",
    "inter_chunk_latencies_seconds",
    "error",
)


def _client_index(client_id: str, count: int) -> int:
    suffix = client_id.rsplit("-", 1)[-1]
    return int(suffix) % count if suffix.isdigit() else 0


def _rate(args: argparse.Namespace, index: int) -> float:
    if args.client_index is not None:
        if args.scenario == "balanced":
            return args.total_rps / args.client_count
        if args.client_count < 2:
            raise ValueError("the hot scenario requires at least two clients")
        return (
            args.total_rps * 0.8
            if index == 0
            else args.total_rps * 0.2 / (args.client_count - 1)
        )
    return args.total_rps


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


async def _send_one(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    client_id: str,
    index: int,
    sequence: int,
    planned_at: float,
    run_started: float,
) -> dict[str, Any]:
    await asyncio.sleep(max(0, planned_at - time.perf_counter()))
    sent_at = time.perf_counter()
    request_id = f"{args.run_id}-{index}-{sequence}"
    session_id = f"{args.run_id}-session-{index}"
    row: dict[str, Any] = {
        "run_id": args.run_id,
        "client_id": client_id,
        "sequence": sequence,
        "planned_at_utc": datetime.fromtimestamp(
            time.time() + planned_at - time.perf_counter(), timezone.utc
        ).isoformat(),
        "planned_offset_seconds": round(planned_at - run_started, 6),
        "sent_at_utc": _utc(),
        "sent_offset_seconds": round(sent_at - run_started, 6),
        "first_chunk_offset_seconds": "",
        "completed_offset_seconds": "",
        "status_code": "",
        "outcome": "client_error",
        "request_id": request_id,
        "session_id": session_id,
        "pod": "",
        "model_version": "",
        "backend_mode": "simulated",
        "prefix_cache": "",
        "queue_wait_seconds": "",
        "output_chunks": 0,
        "inter_chunk_latencies_seconds": "[]",
        "error": "",
    }
    inter_chunk_times: list[float] = []
    previous_chunk_at: float | None = None
    payload = {
        "prompt": f"benchmark-prompt-{sequence % 10}",
        "session_id": session_id,
        "history": [{"role": "user", "content": f"control-{index}-{sequence % 10}"}],
        "prefix": f"run-{hashlib.sha256(args.run_id.encode()).hexdigest()[:8]}-client-{index}",
        "max_new_tokens": args.max_new_tokens,
    }
    try:
        async with client.stream(
            "POST",
            f"{args.url.rstrip('/')}/generate",
            json=payload,
            headers={"X-Request-ID": request_id},
        ) as response:
            row["status_code"] = response.status_code
            if response.status_code != 200:
                row["outcome"] = "rejected" if response.status_code >= 400 else "client_error"
                row["error"] = (await response.aread()).decode("utf-8", errors="replace")[:256]
                return row
            event = ""
            saw_done = False
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    if event == "meta":
                        row["pod"] = data.get("pod", "")
                        row["model_version"] = data.get("model_version", "")
                        row["backend_mode"] = data.get("backend_mode", "simulated")
                        row["prefix_cache"] = data.get("prefix_cache", "")
                        row["queue_wait_seconds"] = data.get("queue_wait_seconds", "")
                    elif event == "chunk":
                        chunk_at = time.perf_counter()
                        if previous_chunk_at is None:
                            row["first_chunk_offset_seconds"] = round(
                                chunk_at - run_started, 6
                            )
                        else:
                            inter_chunk_times.append(round(chunk_at - previous_chunk_at, 6))
                        previous_chunk_at = chunk_at
                        row["output_chunks"] += 1
                    elif event == "done":
                        saw_done = True
            row["completed_offset_seconds"] = round(time.perf_counter() - run_started, 6)
            row["outcome"] = "completed" if saw_done else "aborted"
            row["inter_chunk_latencies_seconds"] = json.dumps(
                inter_chunk_times, separators=(",", ":")
            )
            if not saw_done:
                row["error"] = "stream ended before the done event"
    except httpx.TimeoutException as exc:
        row["outcome"] = "timed_out"
        row["error"] = type(exc).__name__
    except (httpx.TransportError, json.JSONDecodeError) as exc:
        row["outcome"] = "aborted"
        row["error"] = type(exc).__name__
    return row


async def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    client_id = args.client_id or socket.gethostname()
    index = args.client_index
    if index is None:
        return await _run_virtual_clients(args, client_id)
    if not 0 <= index < args.client_count:
        raise ValueError("client-index must be between zero and client-count minus one")
    rate = _rate(args, index)
    return await _run_client(args, client_id, index, rate)


async def _run_virtual_clients(
    args: argparse.Namespace,
    client_id: str,
) -> list[dict[str, Any]]:
    clients = [
        httpx.AsyncClient(
            timeout=httpx.Timeout(args.timeout),
            limits=httpx.Limits(max_connections=args.concurrency),
            trust_env=False,
            headers={"X-Load-Client": f"client-{index}"},
        )
        for index in range(args.client_count)
    ]
    started = time.perf_counter()
    tasks: list[asyncio.Task[dict[str, Any]]] = []
    sequence = 0
    try:
        while sequence / args.total_rps < args.duration:
            planned_at = started + sequence / args.total_rps
            if args.scenario == "hot":
                index = 0 if sequence % 5 < 4 else 1 + (sequence // 5) % (args.client_count - 1)
            else:
                index = sequence % args.client_count
            tasks.append(asyncio.create_task(_send_one(
                clients[index],
                args,
                f"client-{index}",
                index,
                sequence,
                planned_at,
                started,
            )))
            sequence += 1
            await asyncio.sleep(max(0, started + sequence / args.total_rps - time.perf_counter()))
        return await asyncio.gather(*tasks)
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


async def _run_client(
    args: argparse.Namespace,
    client_id: str,
    index: int,
    rate: float,
) -> list[dict[str, Any]]:
    if rate <= 0:
        return []
    semaphore = asyncio.Semaphore(args.concurrency)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(args.timeout),
        limits=httpx.Limits(max_connections=args.concurrency),
        trust_env=False,
        headers={"X-Load-Client": client_id},
    )
    started = time.perf_counter()
    tasks: list[asyncio.Task[dict[str, Any]]] = []
    sequence = 0

    async def scheduled_request(planned_at: float, request_sequence: int) -> dict[str, Any]:
        async with semaphore:
            return await _send_one(
                client,
                args,
                client_id,
                index,
                request_sequence,
                planned_at,
                started,
            )

    try:
        while sequence / rate < args.duration:
            planned_at = started + sequence / rate
            tasks.append(asyncio.create_task(scheduled_request(planned_at, sequence)))
            sequence += 1
            await asyncio.sleep(max(0, started + sequence / rate - time.perf_counter()))
        return await asyncio.gather(*tasks)
    finally:
        await client.aclose()


def _write_results(args: argparse.Namespace, rows: list[dict[str, Any]]) -> None:
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    for row in rows:
        row["scenario"] = row.get("scenario") or args.scenario
        row["policy"] = row.get("policy") or getattr(args, "policy", "normal")
    with output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    counts = {name: 0 for name in (
        "planned", "sent", "completed", "rejected", "aborted", "timed_out", "client_error"
    )}
    counts["planned"] = len(rows)
    for row in rows:
        if row["sent_at_utc"]:
            counts["sent"] += 1
        counts[row["outcome"]] += 1
    successful = [row for row in rows if row["outcome"] == "completed"]
    ttfc = [
        float(row["first_chunk_offset_seconds"]) - float(row["sent_offset_seconds"])
        for row in successful
        if row["first_chunk_offset_seconds"] != ""
    ]
    full_response = [
        float(row["completed_offset_seconds"]) - float(row["sent_offset_seconds"])
        for row in successful
        if row["completed_offset_seconds"] != ""
    ]
    queue_wait = [
        float(row["queue_wait_seconds"])
        for row in successful
        if row["queue_wait_seconds"] != ""
    ]
    inter_chunk = [
        interval
        for row in successful
        for interval in json.loads(row["inter_chunk_latencies_seconds"] or "[]")
    ]
    summary = {
        "run_id": args.run_id,
        "backend_mode": "simulated",
        "scenario": args.scenario,
        "policy": getattr(args, "policy", "normal"),
        "duration_seconds": args.duration,
        "total_rps": args.total_rps,
        "counts": counts,
        "successful_latency_seconds": {
            "observations": len(successful),
            "ttfc_p50": _percentile(ttfc, 0.5),
            "ttfc_p95": _percentile(ttfc, 0.95),
            "full_p50": _percentile(full_response, 0.5),
            "full_p95": _percentile(full_response, 0.95),
            "inter_chunk_observations": len(inter_chunk),
            "inter_chunk_p50": _percentile(inter_chunk, 0.5),
            "inter_chunk_p95": _percentile(inter_chunk, 0.95),
            "queue_observations": len(queue_wait),
            "queue_p50": _percentile(queue_wait, 0.5),
            "queue_p95": _percentile(queue_wait, 0.95),
            "error_ratio": (counts["rejected"] + counts["aborted"] + counts["timed_out"] + counts["client_error"])
            / counts["sent"] if counts["sent"] else None,
        },
        "output": str(output),
    }
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if getattr(args, "stdout_csv", False):
        print("HW11_CSV_BEGIN", flush=True)
        print(output.read_text(encoding="utf-8"), end="", flush=True)
        print("HW11_CSV_END", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture per-request streaming metrics from the HW11 simulator.")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--total-rps", type=float, default=2)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--client-count", type=int, default=4)
    parser.add_argument("--client-index", type=int, default=os.getenv("CLIENT_INDEX"))
    parser.add_argument("--client-id", default=os.getenv("CLIENT_ID", ""))
    parser.add_argument("--scenario", choices=("balanced", "hot"), default="balanced")
    parser.add_argument("--max-new-tokens", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:12])
    parser.add_argument("--output", default="results/requests.csv")
    parser.add_argument("--stdout-csv", action="store_true")
    args = parser.parse_args()
    if args.duration <= 0 or args.total_rps <= 0:
        parser.error("duration and total-rps must be positive")
    if args.concurrency < 1 or args.client_count < 1:
        parser.error("concurrency and client-count must be positive")
    if not 1 <= args.max_new_tokens <= 32:
        parser.error("max-new-tokens must be from 1 to 32")
    if args.client_index is None and args.scenario == "hot" and args.client_count < 2:
        parser.error("the hot scenario requires at least two clients")
    if args.client_index is None and args.client_id:
        args.client_index = _client_index(args.client_id, args.client_count)
    return args


if __name__ == "__main__":
    parsed = parse_args()
    _write_results(parsed, asyncio.run(run(parsed)))

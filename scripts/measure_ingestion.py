"""Measure POST /tickets latency using the official load-generator contract.

This companion does not replace the official generator. It uses the same dataset,
burst cadence, timeout and retry rules, while recording one latency per HTTP
attempt so percentile results are based on observations rather than estimates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

TIMEOUT_SECONDS = 2.0
MAX_ATTEMPTS = 3


def percentile(values: list[float], percentile_value: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile_value * len(ordered)) - 1)
    return ordered[rank]


async def send_ticket(
    client: httpx.AsyncClient,
    *,
    target_url: str,
    ticket: dict[str, Any],
    latencies_ms: list[float],
) -> dict[str, Any]:
    status: int | str | None = None
    attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        started_at = time.perf_counter()
        try:
            response = await client.post(
                target_url,
                json=ticket,
                timeout=TIMEOUT_SECONDS,
            )
            status = response.status_code
            latencies_ms.append((time.perf_counter() - started_at) * 1000)
            if response.status_code < 500:
                break
        except httpx.TimeoutException:
            latencies_ms.append((time.perf_counter() - started_at) * 1000)
            status = "timeout"
        except httpx.HTTPError as error:
            latencies_ms.append((time.perf_counter() - started_at) * 1000)
            status = f"connection_error:{type(error).__name__}"
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(1.0)
    return {
        "external_id": ticket.get("external_id"),
        "attempts": attempts,
        "status": status,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    tickets = [
        json.loads(line)
        for line in args.dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit is not None:
        tickets = tickets[: args.limit]

    latencies_ms: list[float] = []
    tasks: list[asyncio.Task[dict[str, Any]]] = []
    started_at = time.perf_counter()
    async with httpx.AsyncClient() as client:
        for offset in range(0, len(tickets), args.burst):
            burst_started_at = time.perf_counter()
            tasks.extend(
                asyncio.create_task(
                    send_ticket(
                        client,
                        target_url=args.target_url,
                        ticket=ticket,
                        latencies_ms=latencies_ms,
                    )
                )
                for ticket in tickets[offset : offset + args.burst]
            )
            remaining = 1.0 - (time.perf_counter() - burst_started_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        results = await asyncio.gather(*tasks)

    statuses = Counter(str(result["status"]) for result in results)
    return {
        "dataset": str(args.dataset.resolve()),
        "target_url": args.target_url,
        "tickets_sent": len(tickets),
        "accepted": sum(
            isinstance(result["status"], int) and result["status"] < 300
            for result in results
        ),
        "rejected_4xx": sum(
            isinstance(result["status"], int) and 400 <= result["status"] < 500
            for result in results
        ),
        "failed": sum(
            not isinstance(result["status"], int) or result["status"] >= 500
            for result in results
        ),
        "retries": sum(result["attempts"] - 1 for result in results),
        "status_counts": dict(sorted(statuses.items())),
        "http_attempts": len(latencies_ms),
        "duration_seconds": round(time.perf_counter() - started_at, 6),
        "latency_ms": {
            "p50": percentile(latencies_ms, 0.50),
            "p95": percentile(latencies_ms, 0.95),
            "p99": percentile(latencies_ms, 0.99),
            "max": max(latencies_ms, default=None),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--target-url", default="http://localhost:8000/tickets")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--burst", type=int, default=50)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = asyncio.run(run(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")


if __name__ == "__main__":
    main()

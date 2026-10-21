from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from triagem.core.config import get_settings
from triagem.core.database import async_session_factory, engine
from triagem.job_recovery import requeue_failed_classification_jobs


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Requeue failed classification jobs without deleting their history."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--external-id", help="Requeue the latest failed job for one ticket.")
    target.add_argument(
        "--all",
        action="store_true",
        dest="all_failed",
        help="Requeue the latest failed classification job for every eligible ticket.",
    )
    return parser.parse_args(argv)


async def _run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    try:
        requeued = await requeue_failed_classification_jobs(
            async_session_factory,
            external_id=None if args.all_failed else args.external_id,
            max_attempts=settings.classification_max_attempts,
        )
    finally:
        await engine.dispose()

    for item in requeued:
        print(
            f"requeued external_id={item.external_id} "
            f"failed_job_id={item.failed_job_id} new_job_id={item.new_job_id}"
        )
    print(f"requeued_count={len(requeued)}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()

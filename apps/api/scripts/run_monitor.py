"""Run one regulatory-intelligence cycle from the command line or cron.

python -m scripts.run_monitor                     # detect since the last run, then notify
python -m scripts.run_monitor --no-notify         # detect only
python -m scripts.run_monitor --since 2026-09-01  # rescan an explicit window
python -m scripts.run_monitor --baseline Ozempic --baseline Wegovy
"""

import argparse
import asyncio
from datetime import date

from app.db.session import engine
from app.intelligence.monitor import get_regulatory_monitor
from app.intelligence.service import get_notification_dispatcher, run_cycle


async def main(args: argparse.Namespace) -> None:
    monitor = get_regulatory_monitor()
    try:
        if args.baseline:
            captured = await monitor.capture_label_baselines(args.baseline)
            print(f"Captured {captured} new label baselines for: {', '.join(args.baseline)}")
            return

        dispatcher = None if args.no_notify else get_notification_dispatcher()
        summary, dispatched = await run_cycle(
            monitor, dispatcher, since=args.since, until=args.until
        )
        print(f"Monitor {summary.status} in {(summary.completed_at - summary.started_at).seconds}s")
        for outcome in summary.outcomes:
            line = (
                f"  {outcome.detector:<24} {outcome.status:<10} "
                f"{outcome.window_start} -> {outcome.window_end}  "
                f"scanned={outcome.records_scanned:<5} new_events={outcome.events_created:<4} "
                f"baselines={outcome.baselines_recorded}"
            )
            print(line + (f"  error={outcome.error}" if outcome.error else ""))
        if dispatched is not None:
            print(
                f"Notifications: {dispatched.watches_checked} watches checked, "
                f"{dispatched.digests_sent} digests sent, "
                f"{dispatched.events_delivered} events delivered"
            )
            for failure in dispatched.failures:
                print(f"  failed: {failure}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--since", type=date.fromisoformat, help="Window start (YYYY-MM-DD)")
    parser.add_argument("--until", type=date.fromisoformat, help="Window end (YYYY-MM-DD)")
    parser.add_argument("--no-notify", action="store_true", help="Detect without notifying")
    parser.add_argument(
        "--baseline", action="append", metavar="DRUG", help="Capture label baselines only"
    )
    asyncio.run(main(parser.parse_args()))

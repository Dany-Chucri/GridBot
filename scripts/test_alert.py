#!/usr/bin/env python3
"""Send one-off test alerts through whatever channel is configured.

Loads config exactly the way `gridbot.main` does (config/gridbot.yaml +
environment secrets) and fires real messages through
`gridbot.alerting.build_alert_callback`, so a successful run means the
soak's actual alert path — not just the HTTP call in isolation — works.

By default sends one message at each of the three severities the bot
actually uses (INFO, WARNING, CRITICAL) so you can confirm both delivery
and `alerting.min_severity` filtering in one pass — e.g. with the default
min_severity=WARNING, the CRITICAL and WARNING messages should arrive and
the INFO one should not.

Usage:
    python scripts/test_alert.py
    python scripts/test_alert.py --config /etc/gridbot/gridbot.yaml
    python scripts/test_alert.py --severity CRITICAL   # send just one
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gridbot.alerting import _SEVERITY_ORDER, build_alert_callback
from gridbot.config import load_config

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

ALL_SEVERITIES = ["INFO", "WARNING", "CRITICAL"]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-c", "--config", type=Path, default=Path("config/gridbot.yaml"))
    parser.add_argument(
        "--severity",
        choices=ALL_SEVERITIES,
        help="Send only this one severity instead of all three",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    callback = build_alert_callback(config.alerting)

    if callback is None:
        print(
            "No alert channel is enabled+configured. Check `alerting:` in "
            f"{args.config} (channel enabled) and the matching env var "
            "(GRIDBOT_TELEGRAM_BOT_TOKEN / GRIDBOT_DISCORD_WEBHOOK_URL)."
        )
        return 1

    min_severity = config.alerting.min_severity.upper()
    print(f"alerting.min_severity = {min_severity}")

    severities = [args.severity] if args.severity else ALL_SEVERITIES
    for severity in severities:
        expected = _SEVERITY_ORDER.get(severity, 0) >= _SEVERITY_ORDER.get(min_severity, 0)
        note = "expect delivery" if expected else "expect suppression (below min_severity)"
        print(f"Sending a {severity} test alert... ({note})")
        await callback(severity, f"test alert from scripts/test_alert.py ({severity})")

    print(
        "Done — check the Telegram chat / Discord channel now against the "
        "'expect' notes above. If a message that should have been delivered "
        "didn't arrive, look for a 'Telegram/Discord alert failed' ERROR line "
        "above (bad token/webhook/chat_id) rather than an exception here — "
        "delivery failures are logged, not raised."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

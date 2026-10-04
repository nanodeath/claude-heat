"""Show which Claude Code sessions are driving CPU load in their tab titles."""

import argparse
import sys
from pathlib import Path

from . import config, monitor


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-heat",
        description="Mark CPU-heavy Claude Code sessions in their terminal tab titles.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"settings file (default: {config.default_path()})",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="print each session's current load once and exit",
    )
    args = parser.parse_args()

    try:
        settings = config.load(args.config)
    except config.ConfigError as e:
        sys.exit(f"claude-heat: {e}")

    if args.print:
        monitor.print_once(settings)
    else:
        monitor.run(settings)

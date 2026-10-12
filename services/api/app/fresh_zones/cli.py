"""Command-line entry points.

    python -m app.fresh_zones.cli nightly [--symbols A,B] [--all]
    python -m app.fresh_zones.cli monitor [--loop]
    python -m app.fresh_zones.cli opportunities [--limit 20]
    python -m app.fresh_zones.cli replay SYMBOL --start 2023-01-01 [--end 2024-12-31] [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from app.fresh_zones.service import get_engine, universe_symbols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fresh-zones")
    sub = parser.add_subparsers(dest="command", required=True)
    nightly = sub.add_parser("nightly", help="rebuild zones after the close")
    nightly.add_argument("--symbols", help="comma-separated symbols (default: configured universe)")
    nightly.add_argument("--all", action="store_true", help="ignore universe filters")
    monitor = sub.add_parser("monitor", help="run live monitor cycle(s)")
    monitor.add_argument("--loop", action="store_true")
    monitor.add_argument("--interval", type=int, default=60)
    opp = sub.add_parser("opportunities", help="ranked fresh zones near price")
    opp.add_argument("--limit", type=int, default=20)
    rep = sub.add_parser("replay", help="walk-forward end-of-day replay for one symbol")
    rep.add_argument("symbol")
    rep.add_argument("--start", required=True)
    rep.add_argument("--end")
    rep.add_argument("--out")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    engine = get_engine()
    if args.command == "nightly":
        symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else universe_symbols()
        print(json.dumps(engine.run_nightly(symbols, enforce_universe=not args.all), indent=2, default=str))
    elif args.command == "monitor":
        while True:
            print(json.dumps(engine.monitor_cycle(), default=str))
            if not args.loop:
                break
            time.sleep(args.interval)
    elif args.command == "opportunities":
        print(json.dumps(engine.ranked_opportunities(limit=args.limit), indent=2, default=str))
    elif args.command == "replay":
        from app.fresh_zones.replay import replay

        symbol = args.symbol.upper()
        report = replay(symbol, engine.provider.daily_history(symbol), args.start, args.end, params=engine.params)
        text = json.dumps(report, indent=2, default=str)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"{len(report['events'])} events, {len(report['alerts'])} alerts → {args.out}")
        else:
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

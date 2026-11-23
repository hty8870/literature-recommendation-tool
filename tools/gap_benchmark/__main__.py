from __future__ import annotations

import argparse
import asyncio
import json
import re

from .data import prepare
from .evaluation import audit_records, evaluate
from .runners import engine_evaluation, run_agents


def main() -> int:
    parser = argparse.ArgumentParser(
        description="External scientific literature quality gap benchmark"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    for command in ("engine", "run", "evaluate", "audit"):
        item = commands.add_parser(command)
        item.add_argument("--name", required=True)
        if command in {"run", "audit"}:
            item.add_argument("--concurrency", type=int, choices=range(1, 5), default=3)
        if command == "run":
            item.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if getattr(args, "name", None) and not re.fullmatch(r"[a-zA-Z0-9_-]+", args.name):
        raise ValueError("run name must not contain paths")
    if args.command == "prepare":
        result = prepare()
    elif args.command == "engine":
        result = engine_evaluation(args.name)
    elif args.command == "run":
        result = asyncio.run(run_agents(args.name, args.concurrency, resume=args.resume))
    elif args.command == "evaluate":
        result = evaluate(args.name)
    else:
        result = asyncio.run(audit_records(args.name, args.concurrency))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

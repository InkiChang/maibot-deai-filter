"""Local CLI for the opt-in pending-candidate Unix socket viewer."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from candidate_viewer import request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read pending MaiBot memory candidates")
    parser.add_argument("--socket", required=True, help="Unix socket path")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("list", help="list pending candidates")
    detail = sub.add_parser("get", help="show one pending candidate")
    detail.add_argument("candidate_id")
    args = parser.parse_args(argv)
    payload = {"version": 1, "op": "list_pending", "limit": 20, "cursor": None}
    if args.operation == "get":
        payload = {"version": 1, "op": "get_pending", "candidate_id": args.candidate_id}
    try:
        result = asyncio.run(request(args.socket, payload))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"candidate viewer unavailable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())

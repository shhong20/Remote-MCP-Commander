from __future__ import annotations

import argparse
from pathlib import Path

from remote_mcp_commander.gateway.audit import AuditJournal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect Remote MCP Commander audit journals.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="verify retained audit hash chains")
    verify.add_argument("path", type=Path)
    verify.add_argument("--retention-files", type=int, default=5)
    verify.add_argument("--json", action="store_true")
    return parser


def run() -> None:
    args = build_parser().parse_args()
    journal = AuditJournal(args.path, retention_files=args.retention_files)
    result = journal.verify()
    if args.json:
        print(result.model_dump_json())
    else:
        status = "valid" if result.valid else "invalid"
        print(
            f"{status}: checked={result.checked_records} "
            f"legacy={result.legacy_records} files={result.retained_files}"
        )
        if result.error:
            print(result.error)
    if not result.valid:
        raise SystemExit(1)


if __name__ == "__main__":
    run()

from __future__ import annotations

import argparse

from remote_mcp_commander.policy import pty_approval_target, validate_pty_argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print the approval target bound to an exact PTY argv."
    )
    parser.add_argument("argv", nargs="+", help="exact PTY executable and arguments")
    return parser


def run() -> None:
    args = build_parser().parse_args()
    error = validate_pty_argv(args.argv)
    if error is not None:
        raise SystemExit(error)
    print(pty_approval_target(args.argv))


if __name__ == "__main__":
    run()

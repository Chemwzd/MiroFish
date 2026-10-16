#!/usr/bin/env python3
"""Start a long-running daemon so the calling shell returns *immediately*.

Why this exists
---------------
`nohup ./server > log 2>&1 < /dev/null &` looks detached, but the daemon is
still a descendant of the shell that launched it. Agent tool calls keep
reading the command's output pipe / waiting for its process tree, so a server
that never exits makes the call spin until the tool timeout (minutes), even
though the work already finished.

This launcher uses `start_new_session=True` (setsid semantics) and
`close_fds=True` with stdin/stdout/stderr pinned, so:
  * the tool call returns at once with the daemon PID,
  * the daemon is reparented to pid 1 and survives the caller,
  * no stream of the caller stays open.

Usage
-----
    python3 deploy/daemon.py --log /tmp/mf-backend.log \
        --cwd ~/.hermes/workspace/MiroFish/backend \
        -- ./.venv/bin/python run.py

Then verify in a *separate* short call (never sleep-and-check in the same call):
    lsof -nP -iTCP:5001 -sTCP:LISTEN
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Detached daemon launcher")
    ap.add_argument("--log", required=True, help="append stdout+stderr here")
    ap.add_argument("--cwd", default=None, help="working directory for the daemon")
    ap.add_argument("--env", action="append", default=[], metavar="K=V",
                    help="extra environment variable (repeatable)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- command [args...]")
    a = ap.parse_args(argv)

    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        print("daemon.py: no command given", file=sys.stderr)
        return 2

    log_path = os.path.abspath(os.path.expanduser(a.log))
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    env = dict(os.environ)
    # A daemon must not inherit an active virtualenv unless it asked for one.
    env.pop("VIRTUAL_ENV", None)
    for item in a.env:
        key, _, value = item.partition("=")
        env[key] = value

    log = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(  # noqa: S603 - explicit argv, no shell
            cmd,
            cwd=os.path.expanduser(a.cwd) if a.cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()

    print(f"daemon pid={proc.pid} log={log_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

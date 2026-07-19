"""Two-stage example proving state persists in one sandbox session."""

from __future__ import annotations

import sys
from pathlib import Path

workspace = Path("/workspace")
state_path = workspace / ".prepared-state"


def prepare() -> None:
    message = (workspace / "input" / "message.txt").read_text(encoding="utf-8").strip()
    state_path.write_text(message.upper(), encoding="utf-8")
    print("prepared persistent state")


def finish() -> None:
    prepared = state_path.read_text(encoding="utf-8")
    output = workspace / "output"
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.txt").write_text(f"Result: {prepared}\n", encoding="utf-8")
    print("created output/result.txt from persistent state")


if __name__ == "__main__":
    actions = {"prepare": prepare, "finish": finish}
    try:
        action = actions[sys.argv[1]]
    except (IndexError, KeyError):
        raise SystemExit("usage: python process.py [prepare|finish]") from None
    action()

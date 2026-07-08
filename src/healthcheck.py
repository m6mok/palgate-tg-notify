"""Container healthcheck.

The polling loop writes a deadline timestamp into the heartbeat file on
every cycle; this script exits non-zero once that deadline has passed,
i.e. when the loop stopped beating for any reason.
"""

from os import environ
from pathlib import Path
from sys import exit as sys_exit
from time import time


def is_alive(heartbeat_path: Path, now: float) -> bool:
    try:
        deadline = float(heartbeat_path.read_text().strip())
    except (OSError, ValueError):
        return False
    return now <= deadline


if __name__ == "__main__":
    path = Path(environ.get("HEARTBEAT_FILE", "data/heartbeat"))
    sys_exit(0 if is_alive(path, time()) else 1)

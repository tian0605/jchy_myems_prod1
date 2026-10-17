"""Local worker-loop freshness, not provider success or data completeness."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import cast


HEALTH_PATH = Path(tempfile.gettempdir()) / "jishiyun-worker-health.json"
MAX_AGE_SECONDS = 240


def clear_health(path: Path = HEALTH_PATH) -> None:
    path.unlink(missing_ok=True)


def record_health(path: Path = HEALTH_PATH) -> None:
    payload = json.dumps({"pid": os.getpid(), "monotonic": time.monotonic(), "version": 1})
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def healthy(path: Path = HEALTH_PATH) -> bool:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    value = cast(dict[str, object], value)
    if type(value.get("version")) is not int or value.get("version") != 1:
        return False
    pid, heartbeat = value.get("pid"), value.get("monotonic")
    if type(pid) is not int or pid <= 0 or isinstance(heartbeat, bool) or not isinstance(heartbeat, (float, int)):
        return False
    age = time.monotonic() - heartbeat
    if not 0 <= age <= MAX_AGE_SECONDS:
        return False
    return True


if __name__ == "__main__":
    status = healthy()
    print(json.dumps({"worker_loop_fresh": status, "data_completeness_checked": False}))
    sys.exit(0 if status else 1)

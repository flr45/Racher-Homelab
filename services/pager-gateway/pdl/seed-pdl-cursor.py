#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def seed(log_path: Path) -> bool:
    """Create the initial tail cursor at the current EOF exactly once.

    Older gateway versions did not persist their PDL read position. During the
    upgrade to cursor-based tailing, setting the initial cursor immediately before
    the old web process is replaced gives the new process a safe hand-off point:
    historic log data is not replayed, while lines written during the restart are.
    """
    cursor = log_path.with_name(log_path.name + ".racher-cursor")
    if cursor.exists() or not log_path.exists():
        return False

    stat = log_path.stat()
    payload = {
        "dev": int(stat.st_dev),
        "ino": int(stat.st_ino),
        "offset": int(stat.st_size),
    }
    tmp = cursor.with_name(cursor.name + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, cursor)
    if os.geteuid() == 0:
        os.chown(cursor, stat.st_uid, stat.st_gid)
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} /path/to/pdl.log", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        created = seed(path)
    except OSError as exc:
        print(f"Kunne ikke initialisere PDL-cursor: {exc}", file=sys.stderr)
        return 1
    print("PDL-cursor initialiseret ved nuværende EOF" if created else "PDL-cursor allerede klar/afventer log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

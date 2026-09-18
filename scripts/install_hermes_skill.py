#!/usr/bin/env python3
"""Copy the in-repo skill into the local Hermes skills folder."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "skills" / "claim-checker"
DST = Path.home() / ".hermes" / "skills" / "research" / "claim-checker"


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"missing {SRC}")
    DST.parent.mkdir(parents=True, exist_ok=True)
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)
    print(f"Installed skill -> {DST}")


if __name__ == "__main__":
    main()

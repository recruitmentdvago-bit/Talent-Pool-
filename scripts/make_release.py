#!/usr/bin/env python3
"""Build a clean zip to hand to someone: code and docs only - no database, no
candidate PDFs, no browser profile, no personal settings, no demo data.

    python3 scripts/make_release.py [output.zip]
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "storage", "_archived"}
SKIP_FILES = {"settings.yaml", "settings.ui.yaml"}
EMPTY_DIRS = ["storage/profiles", "storage/pdf_inbox", "storage/exports", "storage/imports",
              "config/jds"]


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "talent-pool.zip"
    stage = Path(tempfile.mkdtemp()) / "talent-pool"
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or p.name in SKIP_FILES or p.suffix in {".pyc", ".db"}:
            continue
        if p.suffix == ".zip":
            continue
        dest = stage / rel
        if p.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
    # the two sample vacancies become examples to copy from, not live data
    jds = stage / "config" / "jds"
    ex = stage / "docs" / "example_vacancies"
    ex.mkdir(parents=True, exist_ok=True)
    for f in jds.glob("*.md") if jds.exists() else []:
        shutil.move(str(f), ex / f.name)
    for d in EMPTY_DIRS:
        (stage / d).mkdir(parents=True, exist_ok=True)
        (stage / d / ".keep").write_text("")
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            z.write(p, Path("talent-pool") / p.relative_to(stage))
    n = sum(1 for _ in stage.rglob("*") if _.is_file())
    print(f"wrote {out}  ({n} files, {out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

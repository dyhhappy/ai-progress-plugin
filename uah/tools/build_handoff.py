"""Build the UAH handoff archive.

Staging layout (inside the zip root ``UAH_handoff_<date>/``)::

    README_HANDOFF.md
    uah/                      full UAH package source (no __pycache__)
    start_uah_hud.cmd
    UAH_ARCHITECTURE.md
    docs/UAH_*.md              the four v2 reports
    screenshots/               real-run screenshots (live_* + smoke set)
    patches/uah_branch.diff    0108a93..HEAD
    patches/00_COMMITS.txt
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(r"E:\UnrealHybridAgent")
STAGE = REPO / "artifacts" / "handoff" / "_stage"
DATE = "2026-09-22"
ROOT_NAME = f"UAH_handoff_{DATE}"

SKIP_DIRS = {"__pycache__", ".git", ".venv", ".venv-mcp", ".pytest_cache", ".mypy_cache"}


def run(cmd: str) -> str:
    p = subprocess.run(cmd, shell=True, cwd=str(REPO), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return (p.stdout or "") + (p.stderr or "")


def copy_tree(src: Path, dst: Path) -> int:
    """Copy a tree, skipping caches. Returns file count."""
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            shutil.copy2(Path(root) / f, dst / rel / f)
            n += 1
    return n


def main() -> int:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    out = STAGE / ROOT_NAME
    out.mkdir(parents=True)

    report: list[str] = []

    # ---- README -----------------------------------------------------------
    shutil.copy2(REPO / "artifacts" / "handoff" / "README_HANDOFF.md",
                 out / "README_HANDOFF.md")
    report.append("README_HANDOFF.md                     1 file")

    # ---- uah/ source ------------------------------------------------------
    n = copy_tree(REPO / "uah", out / "uah")
    report.append(f"uah/                                 {n:>3} files")

    # ---- launcher + architecture doc -------------------------------------
    shutil.copy2(REPO / "start_uah_hud.cmd", out / "start_uah_hud.cmd")
    shutil.copy2(REPO / "UAH_ARCHITECTURE.md", out / "UAH_ARCHITECTURE.md")
    report.append("start_uah_hud.cmd                     1 file")
    report.append("UAH_ARCHITECTURE.md                   1 file")

    # ---- the four v2 docs -------------------------------------------------
    docdir = out / "docs"
    docdir.mkdir()
    docs = ["UAH_V2_AUDIT.md", "UAH_V2_IMPLEMENTATION_REPORT.md",
            "UAH_V2_ACCEPTANCE_REPORT.md", "UAH_UI_V2_REFINEMENT_REPORT.md"]
    for d in docs:
        shutil.copy2(REPO / "docs" / d, docdir / d)
    report.append(f"docs/                                {len(docs):>3} files")

    # ---- screenshots ------------------------------------------------------
    shots = out / "screenshots"
    shots.mkdir()
    live = ["live_compact_working.png", "live_compact_alert_l4.png",
            "desktop_hud_live.png"]
    live_n = 0
    for f in live:
        src = REPO / "artifacts" / f
        if src.exists():
            shutil.copy2(src, shots / f)
            live_n += 1
    smoke_dir = REPO / "artifacts" / "uah_v2_hud"
    smoke_n = 0
    if smoke_dir.exists():
        for f in sorted(os.listdir(smoke_dir)):
            if f.endswith(".png") and not f.startswith("_"):
                shutil.copy2(smoke_dir / f, shots / f)
                smoke_n += 1
    report.append(f"screenshots/  (live {live_n} + smoke {smoke_n}) = {live_n + smoke_n:>3} files")

    # ---- patches ----------------------------------------------------------
    pdir = out / "patches"
    pdir.mkdir()
    diff = run("git diff 0108a93..HEAD")
    (pdir / "uah_branch.diff").write_text(diff, encoding="utf-8")
    commits = run("git log --oneline 0108a93..HEAD")
    head = run("git rev-parse HEAD").strip()
    (pdir / "00_COMMITS.txt").write_text(
        f"# UAH v2 branch snapshot\n"
        f"# repo   : E:/UnrealHybridAgent\n"
        f"# branch : feature/uah-v2\n"
        f"# HEAD   : {head}\n"
        f"# base   : 0108a93 chore(p0.1): checkpoint before human-override architecture correction\n"
        f"# generated: {_dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"# NOTE: not pushed; no Release/tag changes.\n\n{commits}",
        encoding="utf-8")
    report.append(f"patches/uah_branch.diff              {len(diff):>5} chars")
    report.append("patches/00_COMMITS.txt                1 file")

    # ---- zip --------------------------------------------------------------
    zpath = REPO / "artifacts" / f"{ROOT_NAME}.zip"
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(STAGE):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                p = Path(root) / f
                z.write(p, p.relative_to(STAGE))

    print("\n".join(report))
    print()
    print(f"zip      : {zpath}")
    print(f"size     : {zpath.stat().st_size / 1024:.0f} KB")
    with zipfile.ZipFile(zpath) as z:
        bad = z.testzip()
        names = z.namelist()
    print(f"entries  : {len(names)}  (integrity: {'OK' if bad is None else bad})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

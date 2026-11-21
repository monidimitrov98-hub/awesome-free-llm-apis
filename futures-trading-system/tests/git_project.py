"""Isolated, committed copies of the project for validation-integrity tests.

The copies live in pytest temp folders; the real repository, its git history and its
validation ledger are never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSV = "data/samples/SYNTHETIC_MNQ_1m.csv"
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is required for code-identity tests")
_IDENT = ["-c", "user.name=fts-test", "-c", "user.email=fts-test@example.invalid", "-c", "commit.gpgsign=false"]


def copy_project(dest: Path, with_git: bool = True) -> Path:
    proj = dest / "project"
    proj.mkdir(parents=True)
    for name in ("src", "config"):
        shutil.copytree(ROOT / name, proj / name, ignore=shutil.ignore_patterns("__pycache__", "*.local.toml"))
    shutil.copytree(ROOT / "data" / "samples", proj / "data" / "samples")
    for name in ("run_backtest.py", "run_pipeline.py", ".gitignore", "pyproject.toml"):
        shutil.copy2(ROOT / name, proj / name)
    if with_git:
        git(proj, "init", "-q")
        commit_all(proj, "initial")
    return proj


def git(proj: Path, *args: str) -> str:
    return subprocess.run([GIT, *_IDENT, *args], cwd=proj, check=True, capture_output=True, text=True).stdout


def commit_all(proj: Path, message: str) -> None:
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", message)


def cli(proj: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "FTS_LEDGER_PATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep the copy's working tree clean
    return subprocess.run([sys.executable, "run_backtest.py", *args], cwd=proj, env=env,
                          capture_output=True, text=True, timeout=600)


def append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


IS_OOS = ["--csv", CSV, "--in-sample", "2025-03-10", "2025-03-14", "--out-of-sample", "2025-03-17", "2025-03-18"]

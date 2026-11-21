"""Cross-platform offline test runner for Windows PowerShell & Linux CI/Docker.
Enforces APP_ENV=test and TEST_NO_SEND=1 so tests NEVER hit paid APIs or real chats.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    tests_dir = root / "tests"

    if not tests_dir.exists() or not any(tests_dir.rglob("test_*.py")):
        print("[test-guard] No tests found yet; skipping cleanly.")
        return 0

    env = os.environ.copy()
    env["APP_ENV"] = "test"
    env["TEST_NO_SEND"] = "1"
    env["TZ"] = "Asia/Taipei"

    uv_bin = shutil.which("uv")
    if uv_bin:
        cmd = [uv_bin, "run", "pytest", "-q", *sys.argv[1:]]
    else:
        cmd = [sys.executable, "-m", "pytest", "-q", *sys.argv[1:]]

    result = subprocess.run(cmd, cwd=root, env=env)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

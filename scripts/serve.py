"""TAZAMA quick runner: starts fake LLM, FastAPI backend, and Next.js frontend."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
WEB = ROOT / "web"
IS_WINDOWS = platform.system() == "Windows"

VENV_PY = BACKEND / ".venv" / ("Scripts" if IS_WINDOWS else "bin") / ("python.exe" if IS_WINDOWS else "python")
PYTHON_EXE = str(VENV_PY) if VENV_PY.exists() else sys.executable
NPM_CMD = "npm.cmd" if IS_WINDOWS and shutil.which("npm.cmd") else "npm"


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def wait_for_port(port: int, host: str = "127.0.0.1", timeout: float = 15.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if is_port_in_use(port, host):
            return True
        time.sleep(0.3)
    return False


def main() -> None:
    print("=" * 65)
    print("  TAZAMA — Starting Local Demo Stack")
    print("=" * 65)
    print(f"  Python: {PYTHON_EXE}")
    print(f"  NPM:    {NPM_CMD}")
    print()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(BACKEND / "src")

    procs: list[tuple[str, subprocess.Popen[bytes]]] = []

    try:
        # 1. Fake LLM on port 8088
        if is_port_in_use(8088):
            print("[1/3] Fake LLM port 8088 already in use (assuming already running).")
        else:
            print("[1/3] Starting deterministic Fake LLM on http://127.0.0.1:8088 ...")
            p_llm = subprocess.Popen(
                [PYTHON_EXE, "-u", str(ROOT / "scripts" / "fake_llm.py")],
                cwd=str(ROOT),
                env=env,
            )
            procs.append(("Fake LLM", p_llm))
            if wait_for_port(8088, timeout=8.0):
                print("      Fake LLM is ready on port 8088.")
            else:
                print("      Fake LLM started (waiting for bind).")

        # 2. FastAPI backend on port 8000
        if is_port_in_use(8000):
            print("[2/3] API port 8000 already in use (assuming already running).")
        else:
            print("[2/3] Starting FastAPI server on http://127.0.0.1:8000 ...")
            p_api = subprocess.Popen(
                [PYTHON_EXE, "-u", "-m", "uvicorn", "tazama.api.app:asgi", "--factory", "--host", "127.0.0.1", "--port", "8000"],
                cwd=str(BACKEND),
                env=env,
            )
            procs.append(("FastAPI Backend", p_api))
            if wait_for_port(8000, timeout=10.0):
                print("      FastAPI is ready on http://127.0.0.1:8000/docs")
            else:
                print("      FastAPI started (waiting for bind).")

        # 3. Next.js Web dev server on port 3000
        if is_port_in_use(3000):
            print("[3/3] Web console port 3000 already in use (assuming already running).")
        else:
            print("[3/3] Starting Next.js console on http://localhost:3000 ...")
            p_web = subprocess.Popen(
                [NPM_CMD, "run", "dev"],
                cwd=str(WEB),
                env=env,
            )
            procs.append(("Next.js Frontend", p_web))
            if wait_for_port(3000, timeout=15.0):
                print("      Next.js console is ready on http://localhost:3000")
            else:
                print("      Next.js compiling...")

        print()
        print("=" * 65)
        print("  TAZAMA DEMO STACK IS LIVE AND READY FOR YOUR PRESENTATION:")
        print("  * Web Dashboard: http://localhost:3000")
        print("  * API Docs:      http://127.0.0.1:8000/docs")
        print("  * AI Service:    http://127.0.0.1:8088")
        print("=" * 65)
        print("Keep this process running during your demo.")
        print("Press Ctrl+C to stop all services.\n")

        while True:
            time.sleep(1.0)
            # Check if any started process unexpectedly died
            for name, p in procs:
                ret = p.poll()
                if ret is not None and ret != 0:
                    print(f"[Warning] {name} exited with code {ret}")

    except KeyboardInterrupt:
        print("\nStopping TAZAMA services...")
    finally:
        for name, p in procs:
            if p.poll() is None:
                print(f"Terminating {name}...")
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
        print("All services stopped.")


if __name__ == "__main__":
    main()

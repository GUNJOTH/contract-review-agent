"""合同审查智能体启动引导。"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from contract_review_app.config import settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "celery"


def main() -> None:
    parser = argparse.ArgumentParser(description="Start contract review services.")
    parser.add_argument(
        "--role",
        choices=("all", "api", "worker", "beat"),
        default="all",
        help="Process role to start. Default starts API, worker, and beat together.",
    )
    args = parser.parse_args()
    _install_stop_handler()

    if args.role == "api":
        _run_api()
        return
    if args.role == "worker":
        raise SystemExit(_run_external(_worker_command()))
    if args.role == "beat":
        raise SystemExit(_run_external(_beat_command()))

    _run_all()


def _use_reload() -> bool:
    """Windows + Git Bash 下 uvicorn reload 会再拉一个子进程，Ctrl+C 经常打不到。"""
    if os.name == "nt":
        return False
    return bool(settings.DEBUG)


def _run_api() -> None:
    import uvicorn

    reload_enabled = _use_reload()
    if settings.DEBUG and os.name == "nt":
        print(
            "[bootstrap] Windows 下已关闭 uvicorn 热重载，避免 Ctrl+C 停不掉子进程。"
            "改代码后请重新启动。",
            flush=True,
        )
    try:
        uvicorn.run(
            "contract_review_app.main:app",
            host=settings.HOST,
            port=settings.PORT,
            reload=reload_enabled,
            log_level=settings.LOG_LEVEL.lower(),
        )
    except KeyboardInterrupt:
        print("[bootstrap] received interrupt, stopping API...", flush=True)
        _kill_process_tree(os.getpid())


def _run_all() -> None:
    main_path = SOURCE_ROOT / "contract_review_app" / "main.py"
    commands = [
        ("api", [sys.executable, str(main_path), "--role", "api"]),
        ("worker", _worker_command()),
        ("beat", _beat_command()),
    ]

    running: list[tuple[str, subprocess.Popen[str]]] = []
    env = _child_env()

    def stop_all(_signum=None, _frame=None) -> None:
        print("[bootstrap] received interrupt, stopping child processes...", flush=True)
        _stop_children(running)
        raise SystemExit(0)

    if os.name == "nt":
        signal.signal(signal.SIGINT, stop_all)
        signal.signal(signal.SIGBREAK, stop_all)

    try:
        for name, command in commands:
            child = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
            )
            running.append((name, child))
            print(f"[bootstrap] started {name} pid={child.pid}: {' '.join(command)}")

        while running:
            for name, child in running:
                code = child.poll()
                if code is not None:
                    raise RuntimeError(f"{name} exited with code {code}")
            time.sleep(1)
    except KeyboardInterrupt:
        stop_all()
    finally:
        _stop_children(running)


def _worker_command() -> list[str]:
    pool_args = ["-P", "solo"] if os.name == "nt" else []
    return [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "contract_review_app.tasks.celery_app",
        "worker",
        "-l",
        settings.CELERY_LOG_LEVEL.lower(),
        "-Q",
        settings.CELERY_DEFAULT_QUEUE,
        "--without-mingle",
        *pool_args,
    ]


def _beat_command() -> list[str]:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    return [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "contract_review_app.tasks.celery_app",
        "beat",
        "-l",
        settings.CELERY_LOG_LEVEL.lower(),
        "--schedule",
        str(RUNTIME_ROOT / "beat-schedule"),
    ]


def _run_external(command: list[str]) -> int:
    return subprocess.call(command, cwd=PROJECT_ROOT, env=_child_env())


def _install_stop_handler() -> None:
    """Git Bash / Windows 控制台里 Ctrl+C 不一定变成 KeyboardInterrupt。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _handler(ctrl_type: int) -> bool:
            if ctrl_type in {0, 1}:  # CTRL_C_EVENT / CTRL_BREAK_EVENT
                print("[bootstrap] console ctrl, stopping process tree...", flush=True)
                _kill_process_tree(os.getpid())
                return True
            return False

        callback = handler_type(_handler)
        _install_stop_handler.callback = callback  # type: ignore[attr-defined]
        kernel32.SetConsoleCtrlHandler(callback, True)
    except Exception:
        pass


def _stop_children(running: list[tuple[str, subprocess.Popen[str]]]) -> None:
    for name, child in reversed(running):
        if child.poll() is not None:
            continue
        print(f"[bootstrap] stopping {name} pid={child.pid}")
        _kill_process_tree(child.pid)


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its descendants.

    On Windows, ``terminate()`` / ``kill()`` only targets the immediate
    child process. Grandchildren such as ``uvicorn`` reloader -> server
    can become orphaned, so we use ``taskkill /T`` to walk the whole tree.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
            _wait_pid_timeout(pid, 5)
        except (ProcessLookupError, TimeoutError):
            pass
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _wait_pid_timeout(pid: int, timeout: float) -> None:
    """等待进程退出，超时则抛出 TimeoutError。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise TimeoutError(f"pid {pid} did not exit within {timeout}s")


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    current = env.get("PYTHONPATH")
    parts = [str(SOURCE_ROOT)]
    if current:
        parts.append(current)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
================================================================================
Alcove Island 灵动岛一键启动总调度器
功能：自动识别 .venv、清理残留端口、拉起 D-Bus 后端、等待就绪后启动 QML 前端并托管生命周期。
================================================================================
"""

import os
import sys
import time
import socket
import signal
import subprocess
from pathlib import Path

# ANSI 终端彩色高亮输出
class Colors:
    GREEN = "\033[92m"
    BLUE = "\033[94m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

PROJECT_ROOT = Path(__file__).resolve().parent
WS_PORT = 8765

backend_process = None
frontend_process = None

def log_info(msg: str):
    print(f"{Colors.BLUE}[INFO]{Colors.RESET} {msg}")

def log_success(msg: str):
    print(f"{Colors.GREEN}{Colors.BOLD}[OK]{Colors.RESET} {msg}")

def log_warn(msg: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.RESET} {msg}")

def log_err(msg: str):
    print(f"{Colors.RED}{Colors.BOLD}[ERROR]{Colors.RESET} {msg}")

def find_python_interpreter() -> str:
    """自动探测并优先使用虚拟环境中的 Python 解析器"""
    venv_candidates = [
        PROJECT_ROOT / ".venv" / "bin" / "python",
        PROJECT_ROOT.parent / ".venv" / "bin" / "python",
        PROJECT_ROOT / "venv" / "bin" / "python",
    ]
    for candidate in venv_candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable

def is_port_in_use(port: int) -> bool:
    """检查指定本地端口是否正在监听"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0

def kill_port_occupant(port: int):
    """清理遗留的僵尸进程"""
    if is_port_in_use(port):
        log_warn(f"检测到端口 {port} 已被占用，正在清理遗留进程...")
        try:
            cmd = f"fuser -k {port}/tcp 2>/dev/null || true"
            subprocess.run(cmd, shell=True, timeout=2)
            time.sleep(0.5)
        except Exception as e:
            log_err(f"清理端口异常: {e}")

def cleanup(signum=None, frame=None):
    """安全回收前后端所有子进程"""
    global backend_process, frontend_process
    print("\n" + f"{Colors.YELLOW}正在平稳停止灵动岛所有服务...{Colors.RESET}")

    if frontend_process and frontend_process.poll() is None:
        try:
            frontend_process.terminate()
            frontend_process.wait(timeout=1.5)
        except Exception:
            frontend_process.kill()

    if backend_process and backend_process.poll() is None:
        try:
            backend_process.terminate()
            backend_process.wait(timeout=1.5)
        except Exception:
            backend_process.kill()

    log_success("灵动岛服务已完全退出。")
    sys.exit(0)

def wait_for_backend_ready(timeout: float = 6.0) -> bool:
    """等待后端 WebSocket 服务完全就绪"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if is_port_in_use(WS_PORT):
            return True
        time.sleep(0.15)
    return False

def main():
    global backend_process, frontend_process

    # 注册系统中断信号捕获
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    os.chdir(PROJECT_ROOT)

    print("=" * 60)
    print(f"{Colors.BOLD}{Colors.GREEN} 🏝️  Alcove Island 灵动岛全栈环境一键启动器{Colors.RESET}")
    print("=" * 60)

    # 1. 寻找 Python 环境
    python_bin = find_python_interpreter()
    log_info(f"使用 Python 解释器: {Colors.BOLD}{python_bin}{Colors.RESET}")

    # 2. 端口检查与防重
    kill_port_occupant(WS_PORT)

    # 3. 构造后端启动命令
    # 直接以内联脚本方式执行 DynamicIslandBackend 守护，无需手动交互 CLI
    backend_code = """
import asyncio
from core.backend import DynamicIslandBackend

async def start():
    backend = DynamicIslandBackend()
    await backend.initialize()
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(start())
"""

    log_info("正在启动后端 D-Bus 硬件与通知守护进程...")
    backend_process = subprocess.Popen(
        [python_bin, "-u", "-c", backend_code],
        cwd=str(PROJECT_ROOT),
        env=os.environ.copy()
    )

    # 4. 健康检查：确保后端已在 8765 端口开放监听
    log_info("等待后端 IPC 管道建立中...")
    if not wait_for_backend_ready(timeout=6.0):
        log_err("后端未能按时在 ws://127.0.0.1:8765 启动，请检查 dbus 依赖或日志！")
        cleanup()
        return

    log_success("后端 IPC 管道已就绪！")

    # 5. 启动前端 PySide6 / QML 界面渲染主进程
    log_info("正在拉起灵动岛前端渲染界面 (main.py)...")
    frontend_process = subprocess.Popen(
        [python_bin, "-u", "main.py"],
        cwd=str(PROJECT_ROOT),
        env=os.environ.copy()
    )

    log_success(f"{Colors.BOLD}灵动岛系统已全部成功上线！(按 Ctrl+C 可一键停止){Colors.RESET}\n")

    # 6. 守护主进程循环：只要前端关闭，整个环境随之退出
    try:
        while True:
            # 如果前端窗口退出了，主动触发退出清理
            if frontend_process.poll() is not None:
                log_info("检测到前端界面已关闭。")
                break
            # 如果后端异常崩溃，打印警报并退出
            if backend_process.poll() is not None:
                log_err("后端守护进程异常断开！")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()

if __name__ == "__main__":
    main()
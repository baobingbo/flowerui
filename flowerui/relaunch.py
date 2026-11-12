"""「重启系统」的继任者进程：纯 Python 重启，不经过任何 shell / shell 脚本。

由 ``flowerui.services.restart`` 以
``subprocess.Popen([sys.executable, "-m", "flowerui.relaunch", ...], shell=False)``
拉起。旧进程随后让 uvicorn 优雅退出，本进程等监听端口释放后接管服务。

相比旧的 ``bash manage.sh restart`` 链路：

- 不需要 bash / Git Bash / manage.sh，pip 安装模式同样可用；
- 不会 ``taskkill /F /IM python.exe`` 误杀同一机器上的其它 Python 进程；
- 启动前重新做一次配置级联解析，界面刚保存的 host / port 等配置真正生效；
- stdout/stderr 由父进程重定向到日志文件，重启失败有据可查。

注意：继任者**始终以单进程模式**启动 uvicorn。若原服务是用 ``--reload`` /
``--workers>1`` 拉起的（监听 socket 由 supervisor 父进程持有），继任者接管后
不再保留热更新 / 多 worker；需要这些模式时请用命令行重新启动。
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import socket
import sys
import time
from typing import Optional, Tuple

logger = logging.getLogger("flowerui.relaunch")

DEFAULT_WAIT_TIMEOUT = 30.0
POLL_INTERVAL = 0.1
# 端口刚释放时旧进程可能还在跑 lifespan shutdown（shelve 落盘 / disable_events
# 广播）。再等一小会儿，避免新旧进程同时写状态文件。
SETTLE_DELAY = 0.3


def _bind_target(host: str, port: int) -> Tuple[int, tuple]:
    """把配置里的监听地址转成可用于探测 bind 的 ``(family, sockaddr)``。

    ``0.0.0.0`` / ``*`` 直接按 IPv4 通配处理；域名与 IPv6 字面量交给
    getaddrinfo，避免自己处理 IPv6 scope id 等细节。
    """
    host = str(host or "127.0.0.1").strip()
    if host in ("0.0.0.0", "*", ""):
        return socket.AF_INET, ("0.0.0.0", int(port))
    try:
        infos = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError):
        return socket.AF_INET, ("127.0.0.1", int(port))
    if not infos:
        return socket.AF_INET, ("127.0.0.1", int(port))
    family, _socktype, _proto, _canon, sockaddr = infos[0]
    return family, sockaddr


def port_is_free(host: str, port: int) -> bool:
    """尝试独占 bind 一次；成功说明旧进程已经把监听 socket 让出来了。

    刻意**不**设置 SO_REUSEADDR：探测目的是"确认没人在监听"，而不是"想办法绑上去"。
    也刻意不用 ``os.kill(pid, 0)`` 判断进程存亡 —— 在 Windows 上 CPython 的
    ``os.kill`` 对非 CTRL_* 信号会直接 TerminateProcess，sig=0 同样会杀掉进程。
    """
    family, addr = _bind_target(host, port)
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind(addr)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_for_port_free(
    host: str,
    port: int,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    interval: float = POLL_INTERVAL,
) -> bool:
    """轮询等待端口空闲；超时返回 False（此时放弃启动，原服务不受影响）。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if port_is_free(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def resolve_target() -> Tuple[str, int, str]:
    """重新级联解析一次，拿到重启后的 host / port（含刚落盘的 runtime.toml）。

    这里就是本进程的「启动时解析」，逐源 ``✓/×`` 判定在此打印一次；随后
    uvicorn 加载 ``main`` 时 lifespan 会命中 ``config_bootstrap`` 的进程级缓存，
    不会重复解析（否则会把刚应用的值误当成基线）。
    """
    from .config_bootstrap import resolve_from_cli_env

    resolve_from_cli_env(log=True)
    from .core.config import settings

    return str(settings.host), int(settings.port), (
        "debug" if settings.debug else "info"
    )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="FlowerUI 纯 Python 重启继任者进程（由 services.restart 拉起）"
    )
    parser.add_argument("--old-pid", type=int, default=0,
                        help="旧服务进程 PID（仅用于日志）")
    parser.add_argument("--wait-timeout", type=float, default=DEFAULT_WAIT_TIMEOUT,
                        help="等待旧进程释放端口的最长秒数")
    args = parser.parse_args(argv)

    # 父进程已把 stdout/stderr 重定向到日志文件；这里让日志也走 stderr。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info(
        "继任者进程启动 | pid=%s | 旧服务 pid=%s | cwd=%s | python=%s",
        os.getpid(), args.old_pid, os.getcwd(), sys.executable,
    )

    try:
        host, port, log_level = resolve_target()
    except Exception:  # noqa: BLE001 - 重启失败必须留痕
        logger.exception("重新解析启动配置失败，放弃重启")
        return 1

    logger.info("目标监听 %s:%s；等待旧进程释放端口（最多 %.1fs）",
                host, port, args.wait_timeout)
    if not wait_for_port_free(host, port, args.wait_timeout):
        logger.error(
            "等待端口释放超时：旧进程可能没有退出。继任者放弃启动，原服务不受影响。"
        )
        return 1

    time.sleep(SETTLE_DELAY)
    if not port_is_free(host, port):
        logger.error("端口在确认阶段被重新占用，放弃启动")
        return 1

    try:
        importlib.import_module("flowerui.main")  # 预加载，缩短接管窗口
    except Exception:  # noqa: BLE001
        logger.exception("预加载 flowerui.main 失败，放弃重启")
        return 1

    logger.info("端口已释放，开始以单进程模式启动 uvicorn")
    import uvicorn

    uvicorn.run("flowerui.main:app", host=host, port=port, log_level=log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())

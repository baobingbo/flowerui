"""触发后端自身重启（纯 Python，不调用任何 shell / shell 脚本）。

旧实现
------
``subprocess.Popen(["bash", "backend/manage.sh", "restart"])``：

- 依赖 Git Bash 与源码目录里的 manage.sh，pip 安装模式直接不可用；
- ``stop_backend`` 用 ``taskkill /F /IM python.exe`` 会误杀同机其它 Python 进程；
- 脚本输出被 ``DEVNULL`` 吞掉，重启失败无从排查；
- 以 ``--reload`` 开发模式重新拉起，不适合生产。

新实现：两种重启策略
--------------------
``relaunch``（裸机 / 源码 / pip 直接运行，非容器默认）

1. 用 ``sys.executable`` 直接拉起 ``python -m flowerui.relaunch``
   （``shell=False``，Windows ``DETACHED_PROCESS`` / Unix ``start_new_session``，
   stdout/stderr 重定向到日志文件）；
2. 继任者进程等旧服务释放监听端口后，**重新级联解析配置**（界面刚保存的
   host / port 等改动真正生效）并启动 uvicorn；
3. 当前进程在 HTTP 响应发出后让 uvicorn 优雅退出
   （``Server.should_exit = True``），从而执行 FastAPI lifespan shutdown
   （事件状态还原、状态落盘等），不会硬杀进程。

``exit``（容器 / 编排器托管，容器内默认）

**绝不 fork 继任者**。容器把服务作为主进程启动时，主进程退出即容器终止，
任何子进程都会被一并杀掉；正确做法是只让当前进程优雅退出，由外部重启策略
（Docker ``--restart=always`` / k8s ``restartPolicy: Always`` / systemd /
supervisord）重新拉起服务。注意 Docker 的 ``on-failure`` 不会重拉退出码 0
的进程，而 uvicorn 优雅退出正是 0。

策略由 ``restart_mode``（``FLOWER_RESTART_MODE``）控制：
``auto``（默认，容器自动改用 exit）/ ``relaunch`` / ``exit`` / ``disabled``。
容器判定可用 ``FLOWERUI_IN_CONTAINER=1|0`` 显式覆盖，详见 ``deploy_env``。

环境变量
--------
- ``FLOWER_RESTART_WAIT_TIMEOUT``：继任者等待端口释放的最长秒数，默认 30；
- ``FLOWER_RESTART_LOG``：自定义重启日志路径；默认源码模式写
  ``backend/logs/restart.log``，只读 / pip 模式回退到系统临时目录。

旧的 ``FLOWER_RESTART_SCRIPT`` 已**不再生效**（重启链路不再使用任何脚本）。
"""
from __future__ import annotations

import gc
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import deploy_env

logger = logging.getLogger("flowerui.restart")

RELAUNCH_MODULE = "flowerui.relaunch"
WAIT_TIMEOUT_ENV = "FLOWER_RESTART_WAIT_TIMEOUT"
LOG_PATH_ENV = "FLOWER_RESTART_LOG"
RESTART_MODE_ENV = "FLOWER_RESTART_MODE"
DEFAULT_DELAY = 1.2
DEFAULT_WAIT_TIMEOUT = 30.0

# 重启策略（对外暴露在 /api/system/restart 响应的 mode 字段）
MODE_RELAUNCH = "python-relaunch"
MODE_EXIT = "exit"
MODE_DISABLED = "disabled"
VALID_MODES = ("auto", "relaunch", "exit", "disabled")


def _wait_timeout() -> float:
    try:
        return float(os.environ.get(WAIT_TIMEOUT_ENV, DEFAULT_WAIT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_WAIT_TIMEOUT


def _log_file() -> Path:
    """重启日志位置：优先用户指定；否则源码模式写 backend/logs，pip / 只读回退 tmp。"""
    configured = os.environ.get(LOG_PATH_ENV)
    if configured:
        return Path(configured).expanduser()
    # 本文件位于 backend/flowerui/services/，上溯三级即 backend/
    candidate = Path(__file__).resolve().parent.parent.parent / "logs" / "restart.log"
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        return Path(tempfile.gettempdir()) / "flowerui-restart.log"


def build_command() -> List[str]:
    """构造继任者命令：全程 Python 解释器 + 模块，绝无 bash / sh / manage.sh。"""
    return [
        sys.executable,
        "-m",
        RELAUNCH_MODULE,
        "--old-pid",
        str(os.getpid()),
        "--wait-timeout",
        str(_wait_timeout()),
    ]


def describe_restart_command() -> dict:
    """返回继任者命令详情（仅 ``relaunch`` 策略会真正执行），便于调试。"""
    return {
        "mode": MODE_RELAUNCH,
        "command": build_command(),
        "python": sys.executable,
        "cwd": os.getcwd(),
        "log_file": str(_log_file()),
        "shell": False,
    }


def _configured_mode() -> str:
    """读 ``restart_mode``。优先用启动级联已解析好的 settings，导入失败时回退环境变量。"""
    raw = None
    try:
        from ..core.config import settings
        raw = getattr(settings, "restart_mode", None)
    except Exception:  # pragma: no cover - 极端情况下 core.config 尚未可用
        raw = None
    if raw is None:
        raw = os.environ.get(RESTART_MODE_ENV, "auto")
    return str(raw or "auto").strip().lower()


def plan_restart() -> dict:
    """决定本次「重启系统」采用哪种策略。

    这是唯一的策略决策入口：API 层用它决定是否直接拒绝（``disabled``），
    ``restart_later`` 用它决定是否 fork 继任者。返回值可直接放进响应 / 日志。
    """
    configured = _configured_mode()
    if configured not in VALID_MODES:
        logger.warning("未知 restart_mode=%r，按 auto 处理", configured)
        configured = "auto"

    detection = deploy_env.detect()
    container = bool(detection["container"])
    reasons = list(detection["reasons"] or [])

    if configured == "disabled":
        mode = MODE_DISABLED
        reason = "restart_mode=disabled：界面重启已禁用，请通过编排器 / 进程管理器重启"
    elif configured == "relaunch":
        mode = MODE_RELAUNCH
        reason = "restart_mode=relaunch：显式指定纯 Python 继任者进程接管"
    elif configured == "exit":
        mode = MODE_EXIT
        reason = "restart_mode=exit：显式指定只退出，由外部重启策略拉起"
    elif container:
        mode = MODE_EXIT
        reason = (
            "检测到容器/编排器环境（%s）：服务退出即容器终止，fork 的继任者会被一并杀掉；"
            "本次不创建子进程，由外部重启策略重新拉起"
            % ("、".join(reasons) or "自动探测")
        )
    else:
        mode = MODE_RELAUNCH
        reason = "非容器环境，使用纯 Python 继任者进程原地接管"

    return {
        "mode": mode,
        "configured": configured,
        "container": container,
        "container_source": detection["source"],
        "container_reasons": reasons,
        "reason": reason,
        "command": build_command() if mode == MODE_RELAUNCH else [],
        "log_file": str(_log_file()),
        "shell": False,
    }


def _child_env() -> Dict[str, str]:
    """继承当前环境（FLOWER_* / FLOWERUI_CLI_OVERRIDES / FLOWERUI_APP_CONF 都要带走）。"""
    env = dict(os.environ)
    env["FLOWERUI_RELAUNCHED"] = "1"  # 便于在日志里区分继任者进程
    env.pop("FLOWER_RESTART_SCRIPT", None)  # 旧变量已废弃，避免误导
    return env


def _spawn_successor() -> subprocess.Popen:
    """拉起继任者进程；不使用 shell，输出落到日志文件。"""
    cmd = build_command()
    log_path = _log_file()
    handle = open(str(log_path), "a", encoding="utf-8", errors="replace")
    try:
        handle.write(
            "\n===== %s 重启请求（旧进程 pid=%s）=====\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid())
        )
        handle.write("命令: %s\ncwd: %s\n\n" % (" ".join(cmd), os.getcwd()))
        handle.flush()

        kwargs = dict(
            cwd=os.getcwd(),
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
        )
        if os.name == "nt":
            # 脱离父进程：父进程退出时继任者不受影响，也不会继承控制台。
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(cmd, **kwargs)
    finally:
        # 父进程关掉自己的句柄副本；子进程已持有独立的 OS 句柄。
        handle.close()


def _find_uvicorn_server():
    """定位当前进程里正在运行的 ``uvicorn.Server`` 实例。

    uvicorn 没有全局注册表，而 ASGI app 也拿不到 Server 引用；这里用 gc 遍历
    存活对象找唯一 ``started=True`` 的实例。只在重启时执行一次，开销可接受。
    返回 None 表示当前并非 uvicorn 直接托管（TestClient / gunicorn 等）。
    """
    try:
        import uvicorn
    except Exception:  # pragma: no cover - uvicorn 必然存在，防御性兜底
        return None
    server_cls = getattr(uvicorn, "Server", None)
    if server_cls is None:
        return None
    for obj in gc.get_objects():
        try:
            if isinstance(obj, server_cls) and getattr(obj, "started", False):
                return obj
        except Exception:  # 个别对象的 __class__ 取值可能抛异常
            continue
    return None


def _supervisor_parent_pid() -> Optional[int]:
    """检测 ``--reload`` / ``--workers>1`` 的 supervisor 父进程。

    这两种模式下监听 socket 由 supervisor 父进程 bind 后传给 spawn 出来的服务
    子进程，只让当前 worker 退出并不能释放端口。multiprocessing spawn 子进程的
    argv 带 ``--multiprocessing-fork``，据此识别。
    """
    if not any("--multiprocessing-fork" in str(a) for a in sys.argv):
        return None
    try:
        ppid = os.getppid()
    except (AttributeError, OSError):  # pragma: no cover
        return None
    return ppid if ppid and ppid != os.getpid() else None


def request_exit() -> str:
    """让当前服务进程退出，返回退出方式（便于日志与测试断言）。

    返回 ``none`` 时表示未找到 uvicorn Server，此时**不主动退出**（避免误杀
    非 uvicorn 托管的进程）；继任者会等端口超时后自行放弃，原服务不受影响。
    """
    parent_pid = _supervisor_parent_pid()
    if parent_pid:
        logger.warning(
            "检测到 uvicorn supervisor（--reload/--workers，pid=%s）：将一并终止父进程，"
            "否则监听端口无法释放。该模式下的退出不保证执行 lifespan 清理。",
            parent_pid,
        )
        try:
            # Unix：supervisor 的信号处理器会优雅 shutdown；
            # Windows：os.kill(SIGTERM) 等价于 TerminateProcess，精确作用于该 PID。
            os.kill(parent_pid, signal.SIGTERM)
        except OSError as exc:
            logger.error("终止 supervisor 父进程失败: %s", exc)

    server = _find_uvicorn_server()
    if server is not None:
        # uvicorn 的 main_loop 每 0.1s 检查一次，会走完整 shutdown（含 lifespan）。
        server.should_exit = True
        return "uvicorn-shutdown+supervisor" if parent_pid else "uvicorn-shutdown"
    if parent_pid:
        return "supervisor-terminated"
    logger.error(
        "未找到运行中的 uvicorn Server（可能由 TestClient / gunicorn 托管）："
        "为避免误杀进程，本次不主动退出；继任者等待端口超时后会放弃启动。"
    )
    return "none"


def restart_later(delay: float = DEFAULT_DELAY, plan: Optional[dict] = None) -> dict:
    """在后台延迟后按策略重启，使运行时配置变更生效，并返回本次策略计划。

    ``delay`` 是为了让调用方（前端）先收到 HTTP 200 响应，再真正让服务退出，
    否则连接在切换瞬间被掐断，前端拿不到回包。
    """
    plan = plan or plan_restart()
    mode = plan["mode"]

    def _run() -> None:
        logger.info(
            "重启策略 | mode=%s | configured=%s | container=%s | %s",
            mode, plan["configured"], plan["container"], plan["reason"],
        )
        if mode == MODE_DISABLED:
            logger.warning("界面重启已禁用，未做任何操作（API 层应提前返回 409）")
            return

        if mode == MODE_EXIT:
            # 容器 / 外部管理器托管：绝不 fork 继任者。主进程退出即容器结束，
            # 任何子进程都会被一并杀掉；只优雅退出，交给外部重启策略拉起。
            try:
                time.sleep(delay)
                how = request_exit()
                logger.info("旧进程退出方式: %s（等待容器/进程管理器重新拉起）", how)
                if how == "none":
                    logger.error(
                        "未找到运行中的 uvicorn Server，无法主动退出；"
                        "请确认服务由外部管理器托管且已配置自动重启，否则重启不会生效。"
                    )
            except Exception:  # noqa: BLE001 - 重启失败必须留下痕迹
                logger.exception("exit 模式重启失败")
            return

        try:
            proc = _spawn_successor()
            logger.info(
                "已拉起继任者进程 pid=%s（纯 Python，不经过 shell）｜命令: %s｜日志: %s",
                proc.pid, " ".join(build_command()), _log_file(),
            )
            time.sleep(delay)
            logger.info("旧进程退出方式: %s", request_exit())
        except FileNotFoundError:
            logger.error("Python 解释器不存在: %s", sys.executable)
        except Exception:  # noqa: BLE001 - 重启失败必须留下痕迹
            logger.exception("后端重启失败")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return plan

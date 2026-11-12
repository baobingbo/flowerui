"""「重启系统」不再依赖 shell 脚本的回归测试。

覆盖：命令构造、Popen 参数（shell=False / 脱离父进程 / 日志重定向）、
继任者端口探测、uvicorn 优雅退出、以及 restart_later 的接线。
本文件不真的重启服务，也不启动监听端口上的真实 uvicorn（仅探测 bind）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

import uvicorn

os.environ["FLOWER_RUNTIME_CONFIG_FILE"] = os.path.join(
    tempfile.gettempdir(), "flowerui_restart_test.toml"
)
os.environ["FLOWER_RESTART_LOG"] = os.path.join(
    tempfile.gettempdir(), "flowerui_restart_test.log"
)

from flowerui import relaunch  # noqa: E402
from flowerui.services import restart  # noqa: E402


def test_build_command_is_pure_python() -> None:
    cmd = restart.build_command()
    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "flowerui.relaunch"]
    assert "--old-pid" in cmd
    assert str(os.getpid()) in cmd
    assert "--wait-timeout" in cmd
    # 任何一项参数里都不允许出现脚本 / shell
    for arg in cmd[1:]:
        low = arg.lower()
        assert "manage.sh" not in low
        assert "bash" not in low
        assert ".sh" not in low


def test_legacy_script_env_is_ignored() -> None:
    with mock.patch.dict(os.environ, {"FLOWER_RESTART_SCRIPT": "/tmp/manage.sh"}):
        cmd = restart.build_command()
        assert all("manage.sh" not in a for a in cmd)
        assert "FLOWER_RESTART_SCRIPT" not in restart._child_env()
        assert restart._child_env()["FLOWERUI_RELAUNCHED"] == "1"


def test_describe_restart_command() -> None:
    info = restart.describe_restart_command()
    assert info["mode"] == "python-relaunch"
    assert info["shell"] is False
    assert info["python"] == sys.executable
    assert info["command"][1:3] == ["-m", "flowerui.relaunch"]
    assert info["log_file"]
    assert info["cwd"] == os.getcwd()


def test_spawn_successor_uses_no_shell() -> None:
    fake_proc = SimpleNamespace(pid=424242)
    with mock.patch.object(restart.subprocess, "Popen", return_value=fake_proc) as popen:
        proc = restart._spawn_successor()
    assert proc is fake_proc
    args, kwargs = popen.call_args
    assert args[0] == restart.build_command()
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == os.getcwd()
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["close_fds"] is True
    assert kwargs["env"]["FLOWERUI_RELAUNCHED"] == "1"
    assert "FLOWER_RESTART_SCRIPT" not in kwargs["env"]
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.DETACHED_PROCESS
    else:
        assert kwargs["start_new_session"] is True
    # 日志确实写出了命令，便于排查重启失败
    with open(os.environ["FLOWER_RESTART_LOG"], "r", encoding="utf-8", errors="replace") as f:
        tail = f.read()[-2000:]
    assert "flowerui.relaunch" in tail


def test_bind_target_wildcard() -> None:
    family, addr = relaunch._bind_target("0.0.0.0", 5555)
    assert family == socket.AF_INET
    assert addr == ("0.0.0.0", 5555)


def test_port_probe_and_wait() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        assert relaunch.port_is_free("127.0.0.1", port) is False
        assert relaunch.wait_for_port_free("127.0.0.1", port, timeout=0.3, interval=0.05) is False
    finally:
        holder.close()
    assert relaunch.port_is_free("127.0.0.1", port) is True
    assert relaunch.wait_for_port_free("127.0.0.1", port, timeout=1.0, interval=0.05) is True


def test_request_exit_sets_uvicorn_should_exit() -> None:
    server = uvicorn.Server(uvicorn.Config("flowerui.main:app", host="127.0.0.1", port=0))
    server.started = True
    try:
        assert restart.request_exit() == "uvicorn-shutdown"
        assert server.should_exit is True
    finally:
        server.should_exit = False
        server.started = False


def test_config_is_resolved_only_once_per_process() -> None:
    """继任者解析过一次后，lifespan 再调用应命中缓存，避免把生效值误报成默认值。"""
    from flowerui import config_bootstrap as cb

    saved = cb._PROCESS_RESOLVE_CACHE
    cb._PROCESS_RESOLVE_CACHE = None
    try:
        with mock.patch.object(cb, "resolve", wraps=cb.resolve) as spy:
            cb.resolve_from_cli_env(log=False)
            cb.resolve_from_cli_env(log=True)
            assert spy.call_count == 1
            cb.resolve_from_cli_env(log=False, force=True)
            assert spy.call_count == 2
    finally:
        cb._PROCESS_RESOLVE_CACHE = saved


def test_restart_later_spawns_then_requests_exit() -> None:
    done = threading.Event()

    def fake_exit() -> str:
        done.set()
        return "uvicorn-shutdown"

    with mock.patch.object(restart, "_spawn_successor",
                           return_value=SimpleNamespace(pid=1)), \
         mock.patch.object(restart, "request_exit", fake_exit):
        restart.restart_later(delay=0)
        assert done.wait(3), "restart_later 未在预期时间内触发退出"


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))

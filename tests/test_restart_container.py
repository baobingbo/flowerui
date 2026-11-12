"""容器部署下「重启系统」不能把容器搞挂：部署识别与策略分流回归测试。

容器把服务作为主进程（PID 1）启动时，主进程退出 = 容器结束，fork 出来的
继任者会被一并杀掉。因此容器里必须走 ``exit`` 策略：不创建任何子进程，
只让当前进程优雅退出，交给外部重启策略重新拉起。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("FLOWER_MOCK", "true")
os.environ.setdefault(
    "FLOWER_RUNTIME_CONFIG_FILE",
    os.path.join(tempfile.gettempdir(), "fui_restart_container.toml"),
)
os.environ.setdefault(
    "FLOWER_RESTART_LOG",
    os.path.join(tempfile.gettempdir(), "fui_restart_container.log"),
)

from fastapi.testclient import TestClient  # noqa: E402

from flowerui import main  # noqa: E402
from flowerui.core.config import settings  # noqa: E402
from flowerui.core.security import get_current_user  # noqa: E402
from flowerui.services import deploy_env, restart  # noqa: E402
from flowerui.services import runtime_config as rc  # noqa: E402


# ---------------------------------------------------------------- 探测工具

def _probe_env(**signals):
    """构造隔离的探测环境：默认所有信号关闭，signals 指定打开哪些。

    把每个检测手段做成独立小函数就是为了能在 Windows 上也稳定地测容器场景。
    """
    defaults = {
        "_explicit_override": None,
        "_has_kubernetes_env": False,
        "_has_oci_env": False,
        "_dockerenv_exists": False,
        "_cgroup_hint": "",
        "_is_pid1": False,
    }
    defaults.update(signals)
    return mock.patch.multiple(
        deploy_env,
        **{key: mock.Mock(return_value=value) for key, value in defaults.items()}
    )


def _detection(container: bool, reasons=None) -> dict:
    return {
        "container": container,
        "source": "auto",
        "reasons": list(reasons if reasons is not None
                        else (["KUBERNETES_SERVICE_HOST"] if container else [])),
    }


def _plan(configured: str, container: bool) -> dict:
    with mock.patch.object(restart, "_configured_mode", return_value=configured), \
         mock.patch.object(deploy_env, "detect", return_value=_detection(container)):
        return restart.plan_restart()


# ---------------------------------------------------------------- deploy_env

def test_clean_host_is_not_container() -> None:
    with _probe_env():
        result = deploy_env.detect()
    assert result["container"] is False
    assert result["reasons"] == []


def test_kubernetes_env_marks_container() -> None:
    with _probe_env(_has_kubernetes_env=True):
        result = deploy_env.detect()
    assert result["container"] is True
    assert "KUBERNETES_SERVICE_HOST" in result["reasons"]


def test_dockerenv_cgroup_pid1_marks_container() -> None:
    with _probe_env(_dockerenv_exists=True, _cgroup_hint="kubepods", _is_pid1=True):
        result = deploy_env.detect()
    assert result["container"] is True
    assert "/.dockerenv" in result["reasons"]
    assert "cgroup:kubepods" in result["reasons"]
    assert "pid=1" in result["reasons"]


def test_explicit_override_wins() -> None:
    with mock.patch.dict(os.environ, {"FLOWERUI_IN_CONTAINER": "1"}):
        assert deploy_env.is_container() is True
        assert deploy_env.detect()["source"] == deploy_env.CONTAINER_ENV
    with mock.patch.dict(os.environ, {"FLOWERUI_IN_CONTAINER": "0"}):
        assert deploy_env.is_container() is False
    # 无效值不当作覆盖
    with mock.patch.dict(os.environ, {"FLOWERUI_IN_CONTAINER": "maybe"}), _probe_env():
        assert deploy_env.is_container() is False


# ---------------------------------------------------------------- plan_restart

def test_auto_in_container_uses_exit_without_successor_command() -> None:
    plan = _plan("auto", True)
    assert plan["mode"] == restart.MODE_EXIT
    assert plan["container"] is True
    assert plan["configured"] == "auto"
    assert plan["command"] == [], "容器模式不允许构造继任者命令"
    assert "容器" in plan["reason"]


def test_auto_on_host_uses_python_relaunch() -> None:
    plan = _plan("auto", False)
    assert plan["mode"] == restart.MODE_RELAUNCH
    assert plan["command"][1:3] == ["-m", "flowerui.relaunch"]
    assert plan["shell"] is False


def test_explicit_relaunch_in_container_is_kept() -> None:
    plan = _plan("relaunch", True)
    assert plan["mode"] == restart.MODE_RELAUNCH
    assert plan["command"]


def test_explicit_exit_on_host_is_kept() -> None:
    plan = _plan("exit", False)
    assert plan["mode"] == restart.MODE_EXIT
    assert plan["command"] == []


def test_disabled_mode_has_no_command() -> None:
    plan = _plan("disabled", False)
    assert plan["mode"] == restart.MODE_DISABLED
    assert plan["command"] == []


def test_unknown_configured_mode_falls_back_to_auto() -> None:
    plan = _plan("typo", True)
    assert plan["configured"] == "auto"
    assert plan["mode"] == restart.MODE_EXIT


def test_restart_mode_is_registered_in_config_fields() -> None:
    field = rc.FIELD_MAP.get("restart_mode")
    assert field is not None
    assert set(field.options or []) == {"auto", "relaunch", "exit", "disabled"}
    assert field.editable is False


def test_configured_mode_reads_settings() -> None:
    saved = settings.restart_mode
    try:
        settings.restart_mode = "exit"
        assert restart._configured_mode() == "exit"
        settings.restart_mode = "AUTO"
        assert restart._configured_mode() == "auto"
    finally:
        settings.restart_mode = saved


# ---------------------------------------------------------------- restart_later

def test_restart_later_exit_mode_never_spawns_successor() -> None:
    plan = _plan("auto", True)
    done = threading.Event()
    spawned = []

    def fake_spawn():
        spawned.append(True)
        return SimpleNamespace(pid=999999)

    def fake_exit():
        done.set()
        return "uvicorn-shutdown"

    with mock.patch.object(restart, "_spawn_successor", fake_spawn), \
         mock.patch.object(restart, "request_exit", fake_exit):
        got = restart.restart_later(delay=0, plan=plan)
        assert done.wait(3), "exit 模式应在延迟后请求进程退出"
    assert got["mode"] == restart.MODE_EXIT
    assert spawned == [], "容器模式绝不能 fork 继任者（会随容器一起被杀）"


def test_restart_later_relaunch_mode_spawns_successor() -> None:
    plan = _plan("auto", False)
    done = threading.Event()

    def fake_exit():
        done.set()
        return "uvicorn-shutdown"

    with mock.patch.object(restart, "_spawn_successor",
                           return_value=SimpleNamespace(pid=1)) as spawn, \
         mock.patch.object(restart, "request_exit", fake_exit):
        got = restart.restart_later(delay=0, plan=plan)
        assert done.wait(3)
    assert got["mode"] == restart.MODE_RELAUNCH
    assert spawn.call_count == 1


def test_restart_later_disabled_does_nothing() -> None:
    plan = _plan("disabled", False)
    with mock.patch.object(restart, "_spawn_successor") as spawn, \
         mock.patch.object(restart, "request_exit") as request:
        restart.restart_later(delay=0, plan=plan)
        time.sleep(0.1)  # 给后台线程足够时间跑完（若它错误地执行了）
    spawn.assert_not_called()
    request.assert_not_called()


# ---------------------------------------------------------------- API 层

def _post_restart(plan: dict):
    main.app.dependency_overrides[get_current_user] = lambda: "tester"
    try:
        with mock.patch.object(restart, "plan_restart", return_value=plan), \
             mock.patch.object(restart, "restart_later", return_value=plan) as later:
            response = TestClient(main.app).post("/api/system/restart")
        return response, later
    finally:
        main.app.dependency_overrides.pop(get_current_user, None)


def test_api_disabled_returns_409_and_does_not_restart() -> None:
    response, later = _post_restart(_plan("disabled", False))
    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]
    later.assert_not_called()


def test_api_exit_mode_reports_container_strategy() -> None:
    plan = _plan("auto", True)
    response, later = _post_restart(plan)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == restart.MODE_EXIT
    assert body["container"] is True
    assert body["ok"] is True
    assert "进程管理器" in body["message"]
    assert "容器" in body["detail"]
    assert later.call_args[1]["plan"] is plan


def test_api_relaunch_mode_response() -> None:
    response, _later = _post_restart(_plan("auto", False))
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == restart.MODE_RELAUNCH
    assert response.json()["container"] is False


if __name__ == "__main__":
    import sys
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))

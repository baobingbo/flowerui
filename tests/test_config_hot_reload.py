"""「不重启进程重新应用最新配置」回归测试。

覆盖：热应用分级（已生效 / 需重启 / 仅下次启动）、从配置文件重新读取、
界面编辑不被高优先级来源反向覆盖、运行期钩子（inspect_timeout）、
启动期不提前创建轮询任务、以及 /api/config/reload 的回包结构。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("FLOWER_MOCK", "true")

from fastapi.testclient import TestClient  # noqa: E402

from flowerui import main  # noqa: E402
from flowerui.core.config import settings  # noqa: E402
from flowerui.services import runtime_config as rc  # noqa: E402
from flowerui.services.manager import manager  # noqa: E402

TMP_RUNTIME = os.path.join(tempfile.gettempdir(), "fui_hot_reload_test.toml")


@pytest.fixture
def isolated(monkeypatch):
    """隔离 runtime.toml、settings 与界面覆盖，避免污染同进程其它测试。"""
    path = Path(TMP_RUNTIME)
    # 不用 unlink：本机沙箱 safe-delete 钩子会给 Path.unlink 传 Python 3.7
    # 不支持的 missing_ok 参数。写空文件同样等价于「没有配置文件」。
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("FLOWER_RUNTIME_CONFIG_FILE", TMP_RUNTIME)
    monkeypatch.setattr(rc, "RUNTIME_FILE", path)
    snapshot = {f.key: rc.current_value(f.key) for f in rc.FIELDS}
    saved_overrides = dict(rc._overrides)
    yield path
    for key, value in snapshot.items():
        if key in rc._UI_DEFAULTS:
            rc._ui_state[key] = value
        else:
            setattr(settings, key, value)
    rc._overrides.clear()
    rc._overrides.update(saved_overrides)
    path.write_text("", encoding="utf-8")


def _write(path: Path, **pairs) -> None:
    lines = []
    for key, value in pairs.items():
        if isinstance(value, bool):
            lines.append("%s = %s" % (key, "true" if value else "false"))
        elif isinstance(value, str):
            lines.append('%s = "%s"' % (key, value))
        else:
            lines.append("%s = %s" % (key, value))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 分类常量

def test_hard_restart_keys_cover_structural_fields() -> None:
    for key in ("host", "port", "url_prefix", "cors_origins", "broker_url",
                "mock", "persistent", "db", "enable_events"):
        assert key in rc.HARD_RESTART_KEYS
    for key in ("require_auth",):
        assert key in rc.NEXT_START_ONLY_KEYS
    # 可热更的常见项不能被误标为需重启
    for key in ("debug", "inspect_interval", "inspect_timeout", "auto_refresh",
                "max_workers", "secret_key", "restart_mode"):
        assert key not in rc.HARD_RESTART_KEYS
        assert key not in rc.NEXT_START_ONLY_KEYS


# ---------------------------------------------------------------- hot_reload

def test_hot_reload_applies_new_file_value(isolated) -> None:
    path = isolated
    _write(path, inspect_interval=42000)
    result = rc.hot_reload(log=False)
    assert settings.inspect_interval == 42000.0
    assert "inspect_interval" in result["applied"]
    assert "inspect_interval" not in result["requires_restart"]
    assert result["sources"]["inspect_interval"] == "配置文件"


def test_hot_reload_reports_structural_change_as_requires_restart(isolated) -> None:
    path = isolated
    _write(path, port=6666, debug=True)
    result = rc.hot_reload(log=False)
    assert settings.port == 6666
    assert "port" in result["requires_restart"]
    assert "port" not in result["applied"]
    assert "debug" in result["applied"]


def test_hot_reload_from_environment(isolated, monkeypatch) -> None:
    isolated  # 空文件
    monkeypatch.setenv("FLOWER_INSPECT_INTERVAL", "31000")
    result = rc.hot_reload(log=False)
    assert settings.inspect_interval == 31000.0
    assert result["sources"]["inspect_interval"] == "环境变量"


def test_web_override_is_not_reverted_by_higher_priority_source(isolated, monkeypatch) -> None:
    """界面刚保存的值不应被环境变量 / app.conf 反向覆盖。"""
    rc.update({"inspect_interval": 55000})
    monkeypatch.setenv("FLOWER_INSPECT_INTERVAL", "77000")
    # 模拟当前生效值已偏离界面意图，确保本次热应用确实发生变更
    settings.inspect_interval = 88000.0
    result = rc.hot_reload(log=False)
    assert settings.inspect_interval == 55000.0
    assert rc._overrides.get("inspect_interval") == 55000.0
    assert result["sources"]["inspect_interval"] == "界面编辑"


def test_hot_reload_no_change_reports_empty_lists(isolated) -> None:
    isolated
    first = rc.hot_reload(log=False)  # 对齐当前来源
    second = rc.hot_reload(log=False)
    assert second["changed"] == []
    assert second["requires_restart"] == []
    assert first["ok"] is True


def test_inspect_timeout_hook_updates_live_inspector(isolated, monkeypatch) -> None:
    monkeypatch.setenv("FLOWER_INSPECT_TIMEOUT", "1200")
    rc.hot_reload(log=False)
    assert manager.inspector.timeout == pytest.approx(1.2)


def test_auto_refresh_hook_is_noop_before_startup(isolated) -> None:
    """启动期级联也会触发 auto_refresh 钩子，此时不得提前创建后台任务。"""
    saved_state = manager.state
    saved_task = manager._refresh_task
    try:
        manager.state = None
        manager.set_auto_refresh(True)
        assert manager._refresh_task is saved_task
    finally:
        manager.state = saved_state
        manager._refresh_task = saved_task


# ---------------------------------------------------------------- API

def test_api_reload_returns_classification(isolated) -> None:
    _write(isolated, inspect_interval=26000)
    response = TestClient(main.app).post("/api/config/reload")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert "applied" in body and "requires_restart" in body
    assert "next_start_only" in body and "message" in body
    assert body["config"]["items"]
    assert "inspect_interval" in body["applied"]


def test_api_reload_message_mentions_restart_items(isolated) -> None:
    _write(isolated, port=6777)
    body = TestClient(main.app).post("/api/config/reload").json()
    assert "port" in body["requires_restart"]
    assert "需重启" in body["message"]


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))

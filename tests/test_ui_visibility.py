"""「侧边栏 / 配置模块显示配置」回归测试。

这些字段是服务端权威配置：写入 flowerui.runtime.toml，经 /api/config/ui
下发给前端，所有浏览器一致；不在浏览器本地持久化。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("FLOWER_MOCK", "true")

from fastapi.testclient import TestClient  # noqa: E402

from flowerui import main  # noqa: E402
from flowerui.core.config import settings  # noqa: E402
from flowerui.services import runtime_config as rc  # noqa: E402

VIS_KEYS = ("ui_show_sidebar", "ui_hidden_menus", "ui_hidden_modules")
# 不用 pytest 的 tmp_path：本机 safe-delete 钩子会给 Path.unlink 传 Python 3.7
# 不支持的 missing_ok，导致 fixture 创建临时目录时报错。固定文件 + 写空即可。
TMP_RUNTIME = os.path.join(tempfile.gettempdir(), "fui_ui_visibility_test.toml")


@pytest.fixture
def clean(monkeypatch):
    """隔离 TOML 文件与进程内显示状态，避免污染其它测试。"""
    path = Path(TMP_RUNTIME)
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(rc, "RUNTIME_FILE", path)
    monkeypatch.setenv("FLOWER_RUNTIME_CONFIG_FILE", str(path))
    # hot_reload 会重跑整条级联，故对全部字段做快照，避免污染同进程其它测试
    snapshot = {f.key: rc.current_value(f.key) for f in rc.FIELDS}
    saved = dict(rc._overrides)
    yield path
    for key, value in snapshot.items():
        if key in rc._UI_DEFAULTS:
            rc._ui_state[key] = list(value) if isinstance(value, list) else value
        else:
            setattr(settings, key, value)
    rc._overrides.clear()
    rc._overrides.update(saved)
    path.write_text("", encoding="utf-8")


def test_visibility_fields_are_registered_and_hot_applicable():
    keys = {f.key for f in rc.FIELDS}
    assert set(VIS_KEYS) <= keys
    for field in rc.FIELDS:
        if field.key in VIS_KEYS:
            assert field.group == "ui"
            assert field.editable is True
            assert field.key not in rc.HARD_RESTART_KEYS
            assert field.key not in rc.NEXT_START_ONLY_KEYS


def test_ui_defaults_exposes_visibility():
    body = TestClient(main.app).get("/api/config/ui").json()
    assert body["show_sidebar"] is True
    assert body["hidden_menus"] == []
    assert body["hidden_modules"] == []


def test_update_visibility_applies_and_persists_to_toml(clean):
    rc.update({
        "ui_show_sidebar": False,
        "ui_hidden_menus": ["apply", "monitor"],
        "ui_hidden_modules": ["panel.theme", "tab.celery"],
    })
    body = rc.ui_defaults()
    assert body["show_sidebar"] is False
    assert body["hidden_menus"] == ["apply", "monitor"]
    assert body["hidden_modules"] == ["panel.theme", "tab.celery"]

    text = clean.read_text(encoding="utf-8")
    assert "ui_show_sidebar = false" in text
    assert "apply" in text and "panel.theme" in text


def test_api_update_then_ui_endpoint(clean):
    client = TestClient(main.app)
    res = client.put("/api/config", json={"changes": {
        "ui_show_sidebar": False,
        "ui_hidden_menus": ["apply"],
        "ui_hidden_modules": ["tab.mock"],
    }})
    assert res.status_code == 200, res.text
    body = client.get("/api/config/ui").json()
    assert body["show_sidebar"] is False
    assert body["hidden_menus"] == ["apply"]
    assert body["hidden_modules"] == ["tab.mock"]
    # 关键：显示管理专用键刻意不进入通用配置项列表（describe().items），
    # 避免被「保存修改 / 生成完整配置」当成普通项反复读写而互相打架。
    items = res.json()["config"]["items"]
    assert all(i["key"] not in VIS_KEYS for i in items)
    # 但它们仍注册、仍可由接口写入并生效（经 /api/config/ui 下发）。
    assert set(VIS_KEYS) <= {f.key for f in rc.FIELDS}
    assert rc.ui_defaults()["show_sidebar"] is False


def test_reset_restores_boot_visibility(clean):
    rc.update({"ui_show_sidebar": False, "ui_hidden_menus": ["apply"]})
    assert rc.ui_defaults()["show_sidebar"] is False
    rc.reset(list(VIS_KEYS))
    assert rc.ui_defaults()["show_sidebar"] is True
    assert rc.ui_defaults()["hidden_menus"] == []


def test_hot_reload_keeps_visibility_as_web_intent(clean):
    """界面刚保存的显示配置不应被热应用的级联反向覆盖。"""
    rc.update({"ui_hidden_menus": ["apply"]})
    result = rc.hot_reload(log=False)
    assert rc.ui_defaults()["hidden_menus"] == ["apply"]
    assert result["ok"] is True


def test_materialize_preserves_visibility(clean):
    """生成完整配置（重写整份 TOML）不得冲掉已保存的显示设置。"""
    rc.update({
        "ui_show_sidebar": False,
        "ui_hidden_menus": ["apply", "monitor"],
        "ui_hidden_modules": ["panel.theme", "tab.celery"],
    })
    rc.materialize()
    text = clean.read_text(encoding="utf-8")
    assert "ui_show_sidebar = false" in text
    assert '"apply"' in text and '"monitor"' in text
    assert "panel.theme" in text and "tab.celery" in text
    # 重新加载进程级解析缓存后，生效值仍与保存一致（证明没被默认值覆盖）
    rc.hot_reload(log=False)
    body = rc.ui_defaults()
    assert body["show_sidebar"] is False
    assert body["hidden_menus"] == ["apply", "monitor"]
    assert body["hidden_modules"] == ["panel.theme", "tab.celery"]


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))

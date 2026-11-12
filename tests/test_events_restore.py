"""事件状态还原 + inspect 间隔配置化的回归测试。

不连真实 broker：用假 capp 记录 control.enable_events / disable_events 调用，
验证「只有显式开启还原、且本进程确实改过 worker 状态」才会广播 disable。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from unittest.mock import patch

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.environ.setdefault(
    "FLOWER_RUNTIME_CONFIG_FILE",
    os.path.join(tempfile.gettempdir(), "flowerui_events_restore_test.toml"),
)

from flowerui.core.config import settings
from flowerui.services import manager as manager_mod
from flowerui.services.events import Events
from flowerui.services.manager import ServiceManager
from flowerui.services.runtime_config import FIELDS


class _Control:
    def __init__(self, fail_enable: bool = False):
        self.enable_calls = 0
        self.disable_calls = 0
        self.fail_enable = fail_enable

    def enable_events(self):
        if self.fail_enable:
            raise RuntimeError("simulated broker failure")
        self.enable_calls += 1

    def disable_events(self):
        self.disable_calls += 1


class _App:
    def __init__(self, fail_enable: bool = False):
        self.control = _Control(fail_enable)


def _events(app=None, enabled: bool = False) -> Events:
    ev = Events(app or _App())
    if enabled:
        ev._enable_events()
    return ev


def _run(coro):
    """跑一个协程但不触碰全局 event loop。

    Python 3.7 的 asyncio.run() 结束时会把 current loop 置 None，导致后续
    构造 ServiceManager（Inspector 里的 asyncio.Lock()）抛
    "There is no current event loop"。这里只启用私有 loop，不 set_event_loop。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _shutdown_with(ev: Events, flag: bool) -> None:
    original = settings.disable_events_on_shutdown
    settings.disable_events_on_shutdown = flag
    try:
        mgr = ServiceManager()
        mgr.events = ev
        mgr.mock = None
        mgr._timeline_task = None
        mgr._refresh_task = None
        _run(mgr.shutdown())
    finally:
        settings.disable_events_on_shutdown = original


def test_enable_marks_ownership() -> None:
    app = _App()
    ev = _events(app, enabled=True)
    assert app.control.enable_calls == 1
    assert ev.events_enabled_by_us is True
    ev.disable_events()
    assert app.control.disable_calls == 1
    assert ev.events_enabled_by_us is False


def test_failed_enable_does_not_mark_ownership() -> None:
    ev = _events(_App(fail_enable=True))
    ev._enable_events()
    assert ev.events_enabled_by_us is False


def test_default_shutdown_does_not_disable() -> None:
    app = _App()
    ev = _events(app, enabled=True)
    _shutdown_with(ev, False)
    assert app.control.disable_calls == 0
    assert ev._stop_flag.is_set()


def test_optin_shutdown_disables_only_when_we_enabled() -> None:
    app = _App()
    ev = _events(app, enabled=True)
    _shutdown_with(ev, True)
    assert app.control.disable_calls == 1


def test_optin_shutdown_skips_if_never_enabled() -> None:
    app = _App()
    ev = _events(app)
    _shutdown_with(ev, True)
    assert app.control.disable_calls == 0


def test_inspect_interval_drives_refresh_loop() -> None:
    captured = []

    async def fake_sleep(seconds):
        captured.append(seconds)
        raise asyncio.CancelledError()

    fake_asyncio = types.SimpleNamespace(
        sleep=fake_sleep, CancelledError=asyncio.CancelledError
    )
    mgr = ServiceManager()
    original = settings.inspect_interval
    settings.inspect_interval = 2500.0
    try:
        with patch.object(manager_mod, "asyncio", fake_asyncio):
            _run(mgr._auto_refresh_loop())
    finally:
        settings.inspect_interval = original
    assert captured == [2.5]


def test_new_fields_registered() -> None:
    keys = {f.key for f in FIELDS}
    assert {"inspect_interval", "disable_events_on_shutdown"} <= keys

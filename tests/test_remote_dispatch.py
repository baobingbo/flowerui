"""「下发任务统一远程投递」回归测试。

监控进程不加载业务任务代码：所有下发端点都必须按任务名直接调用
``capp.send_task`` 把消息投递到远程 broker，不得再查询本地注册表或
inspect 到的注册任务列表，也不得因为「本地未注册」返回 404。
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("FLOWER_MOCK", "true")

from fastapi.testclient import TestClient  # noqa: E402

from flowerui import main  # noqa: E402
from flowerui.services.manager import manager  # noqa: E402


class _FakeResult:
    task_id = "remote-task-id"
    state = "PENDING"
    result = None
    traceback = None

    def get(self, *args, **kwargs):
        return self.result


def _run(coro):
    """不用 asyncio.run()：它会清空全局 loop，污染同进程其它测试。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def remote(monkeypatch):
    """把 manager 切到真实路径，并拦截 _run 以记录实际调用的 Celery 方法。"""
    calls = []

    async def fake_run(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return _FakeResult()

    def forbidden(*_a, **_kw):
        raise AssertionError("下发任务不应再查询本地 / inspect 注册列表")

    monkeypatch.setattr(manager, "mock_mode", False)
    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "backend_configured", lambda: False)
    monkeypatch.setattr(manager, "registered_tasks", forbidden)
    return calls


def test_apply_task_always_uses_send_task(remote):
    res = _run(manager.apply_task(
        "app.tasks.not_registered_locally", [1, 2], {"x": 3}, {"queue": "emails"}
    ))
    assert res["task-id"] == "remote-task-id"
    assert len(remote) == 1
    fn, args, kwargs = remote[0]
    assert fn == manager.capp.send_task
    assert args == ("app.tasks.not_registered_locally",)
    assert kwargs["args"] == [1, 2]
    assert kwargs["kwargs"] == {"x": 3}
    assert kwargs["queue"] == "emails"


def test_async_apply_and_send_task_endpoints_are_equivalent(remote):
    client = TestClient(main.app)
    a = client.post(
        "/api/task/async-apply/app.tasks.any", json={"args": [], "kwargs": {}}
    )
    b = client.post(
        "/api/task/send-task/app.tasks.any", json={"args": [], "kwargs": {}}
    )
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    assert a.json()["task-id"] == b.json()["task-id"] == "remote-task-id"
    assert len(remote) == 2
    assert all(call[0] == manager.capp.send_task for call in remote)


def test_apply_endpoint_waits_for_remote_result(remote):
    client = TestClient(main.app)
    res = client.post(
        "/api/task/apply/app.tasks.any?timeout=1", json={"args": [], "kwargs": {}}
    )
    assert res.status_code == 200, res.text
    assert res.json()["task-id"] == "remote-task-id"
    # 第一次投递，第二次从远程 result backend 取结果
    assert len(remote) == 2


def test_apply_task_signature_has_no_registration_gate():
    import inspect

    params = inspect.signature(manager.apply_task).parameters
    assert "require_registered" not in params


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))

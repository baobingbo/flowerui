"""生产加固回归测试：健康检查、全局异常处理、文档暴露开关、强制认证拒启。

这些测试验证「能否上生产」相关的几道安全/运维闸门：
- /healthz 免认证探活；
- 全局 Exception 处理器统一返回 JSON 500 且不泄露细节；
- expose_docs=false 时移除 /docs /redoc /openapi.json 路由；
- require_auth=true 且未启用认证时拒绝启动。
"""
from __future__ import annotations

import os

# 必须在导入 app 前设定，避免真实 broker 连接 / 读取仓库内遗留 runtime.toml
os.environ.setdefault("FLOWER_MOCK", "true")
os.environ.setdefault("FLOWER_RUNTIME_CONFIG_FILE", "/tmp/fui_prod_hardening.toml")

from fastapi.testclient import TestClient  # noqa: E402

from flowerui import main  # noqa: E402
from flowerui.core.config import settings  # noqa: E402


def _add_raising_route() -> None:
    """注册一个必抛异常的路由，并插到静态挂载之前，确保不被 mount('/') 兜底。"""

    def boom():
        raise Exception("boom-for-test")

    main.app.add_api_route("/_raise", boom, methods=["GET"])
    main.app.router.routes.insert(0, main.app.router.routes.pop())


def test_healthz_is_public_and_ok() -> None:
    with TestClient(main.app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200, r.status_code
    assert r.json() == {"status": "ok"}


def test_global_exception_handler_returns_json_500() -> None:
    _add_raising_route()
    # raise_server_exceptions=False 才能拿到处理器返回的 500 体（否则 TestClient 会重抛）
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.get("/_raise")
    assert r.status_code == 500, r.status_code
    assert r.json() == {"detail": "Internal Server Error"}


def test_expose_docs_false_removes_doc_routes() -> None:
    saved = settings.expose_docs
    try:
        settings.expose_docs = False
        main._apply_docs_exposure(main.app)
        paths = {getattr(rt, "path", None) for rt in main.app.router.routes}
        assert "/docs" not in paths
        assert "/redoc" not in paths
        assert "/openapi.json" not in paths
    finally:
        settings.expose_docs = saved
        # 还原：重新构造文档路由（这里仅恢复 settings，路由重建由重启负责，
        # 单测只需确认关闭路径生效；默认开启场景由 test_healthz 等保证）
        settings.expose_docs = True


def test_require_auth_refuses_without_auth() -> None:
    saved_req = settings.require_auth
    saved_enabled = settings.basic_auth_enabled
    try:
        settings.require_auth = True
        settings.basic_auth_enabled = False
        try:
            main._enforce_require_auth()
            raise AssertionError("应当抛出 RuntimeError 拒绝启动")
        except RuntimeError as e:
            assert "require_auth" in str(e)
    finally:
        settings.require_auth = saved_req
        settings.basic_auth_enabled = saved_enabled


def test_require_auth_allows_when_auth_enabled() -> None:
    saved_req = settings.require_auth
    saved_enabled = settings.basic_auth_enabled
    saved_list = settings.basic_auth
    try:
        settings.require_auth = True
        settings.basic_auth_enabled = True
        settings.basic_auth = ["admin:admin"]
        # 不应抛异常
        main._enforce_require_auth()
    finally:
        settings.require_auth = saved_req
        settings.basic_auth_enabled = saved_enabled
        settings.basic_auth = saved_list

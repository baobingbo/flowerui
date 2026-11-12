"""FastAPI 应用入口：装配路由、生命周期、CORS 与前端静态资源。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routers import (
    auth,
    broker,
    config as config_router,
    control,
    dashboard,
    monitor,
    system as system_router,
    tasks,
    themes as themes_router,
    workers,
    ws,
)
from .core.celery_app import mask_url_password
from .core.config import settings
from .services import runtime_config, theme_manager
from .services.manager import manager

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("flowerui")

FRONTEND_DIST = Path(
    os.environ.get(
        "FLOWER_FRONTEND_DIST",
        # 前端产物随后端一起分发：backend/flowerui/static/（frontend/manage.sh build
        # 构建 frontend/dist 后同步到这里，由根 manage.sh run/demo/build 编排）；
        # 开发模式可用 FLOWER_FRONTEND_DIST 指回 frontend/dist 或另指定目录
        Path(__file__).resolve().parent / "static",
    )
)


def _apply_capacity(_value=None) -> None:
    """容量类配置改动后同步到已经建好的 state 对象上，无需重启。"""
    state = getattr(manager, "state", None)
    if state is None:
        return
    state.max_workers_in_memory = settings.max_workers
    state.max_tasks_in_memory = settings.max_tasks


def _apply_debug(value) -> None:
    logging.getLogger().setLevel(logging.DEBUG if value else logging.INFO)


def _apply_inspect_timeout(_value=None) -> None:
    """Inspector 构造时缓存了 timeout，配置热应用后要同步到存活实例。"""
    inspector = getattr(manager, "inspector", None)
    if inspector is not None:
        inspector.timeout = settings.inspect_timeout_seconds


def _apply_auto_refresh(value) -> None:
    """配置热应用时启停后台 inspect 轮询任务。"""
    try:
        manager.set_auto_refresh(bool(value))
    except RuntimeError as exc:
        # 非事件循环上下文（如独立脚本调用 hot_reload）：startup 会按最终值决定
        logger.warning("auto_refresh 热应用跳过（当前无事件循环）: %s", exc)


runtime_config.on_change("max_workers", _apply_capacity)
runtime_config.on_change("max_tasks", _apply_capacity)
runtime_config.on_change("debug", _apply_debug)
runtime_config.on_change("inspect_timeout", _apply_inspect_timeout)
runtime_config.on_change("auto_refresh", _apply_auto_refresh)


def _enforce_require_auth() -> None:
    """require_auth=true 但认证未启用时拒绝启动，防止后台被匿名访问。"""
    if settings.require_auth and not settings.auth_enabled:
        raise RuntimeError(
            "require_auth=true 但认证未启用（basic_auth_enabled=false 且无 basic_auth），"
            "出于安全考虑拒绝启动。请设置 basic_auth_enabled=true 并配置 basic_auth。"
        )


def _apply_docs_exposure(app: FastAPI) -> None:
    """expose_docs=false 时移除 /docs /redoc /openapi.json 路由，避免暴露完整 API schema。"""
    if settings.expose_docs:
        return
    app.docs_url = None
    app.redoc_url = None
    app.openapi_url = None
    # Starlette 的 app.routes 是只读属性，必须用切片就地修改 app.router.routes
    blocked = {"/docs", "/redoc", "/openapi.json"}
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) not in blocked
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时一次性级联解析配置（app.conf > 环境变量 > CLI > runtime.toml >
    # 默认值），每个来源的成功/失败都会打印；运行期不再重新解析连接配置。
    # 继任者重启进程在拉起 uvicorn 前已解析过一次，这里命中进程级缓存直接跳过，
    # 避免同一进程解析两遍把生效值误判成「默认值」。
    from .config_bootstrap import resolve_from_cli_env
    resolve_from_cli_env()
    # 生产安全：require_auth 开启但未启用认证则拒绝启动，防止后台匿名暴露
    _enforce_require_auth()
    # 生产安全：关闭时移除 API 文档路由，避免暴露接口 schema
    _apply_docs_exposure(app)
    runtime_config.load()  # 记录启动值 + 旧版迁移 + 首次模板（不再回放配置）
    theme_manager.start_watcher()  # 启动主题目录监控线程
    await manager.startup()
    _apply_capacity()
    logger.info(
        "FlowerUI 已启动 | 模式=%s | broker=%s",
        "演示" if manager.mock_mode else "真实集群",
        # 启动日志会落盘留存，密码段必须脱敏（与 /api/system 保持同一口径）
        mask_url_password(settings.broker_url),
    )
    # 部署形态 / 重启策略是容器场景最容易踩坑的一项：容器里 fork 继任者会随
    # 容器一起被杀。启动时明确告知本次会采用哪种策略，便于运维核对重启策略。
    from .services import restart as restart_service
    try:
        plan = restart_service.plan_restart()
        logger.info(
            "重启策略 | mode=%s | 容器环境=%s | %s",
            plan["mode"], "是" if plan["container"] else "否", plan["reason"],
        )
    except Exception:  # noqa: BLE001 - 探测失败不能影响服务本身启动
        logger.exception("计算重启策略失败（不影响服务启动）")
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(
    title="FlowerUI - Celery 实时监控",
    description="Flower 的 FastAPI + Vue 3 前后端分离重构版，功能对齐 Flower 2.0.1。",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (
    auth.router,
    config_router.router,
    workers.router,
    tasks.router,
    control.router,
    broker.router,
    dashboard.router,
    monitor.router,
    system_router.router,
    themes_router.router,
    ws.router,
):
    app.include_router(r)


@app.exception_handler(404)
async def spa_fallback(request, exc):
    """前端使用 history 路由，非 API 的 404 交还 index.html。"""
    path = request.url.path
    if path.startswith(("/api", "/ws", "/docs", "/redoc", "/openapi.json", "/metrics")):
        return JSONResponse({"detail": getattr(exc, "detail", "Not Found")}, status_code=404)
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": "前端未构建，请先执行 npm run build"}, status_code=404)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """存活探针：无需认证，供反向代理 / 编排器探活（k8s LivenessProbe 等）。"""
    return {"status": "ok"}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """全局未处理异常兜底：统一返回 JSON 并记录结构化日志，不向客户端泄露细节。"""
    logger.exception("未处理的异常: %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="static")
    logger.info("已挂载前端静态资源: %s", FRONTEND_DIST)
else:
    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "name": "FlowerUI",
            "docs": "/docs",
            "hint": "前端尚未构建，运行 cd frontend && npm install && npm run build",
        }

"""系统级操作：重启后端进程等。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.security import get_current_user
from ...services import restart as restart_service

router = APIRouter(tags=["system"])


class RestartResponse(BaseModel):
    ok: bool = True
    message: str = "后端正在重启，请稍候刷新页面"
    mode: str = restart_service.MODE_RELAUNCH
    container: bool = False
    detail: str = ""


@router.post("/api/system/restart", response_model=RestartResponse)
async def restart_system(_user: str = Depends(get_current_user)) -> RestartResponse:
    """重启后端 API 进程，使需要重启才生效的配置（监听端口、CORS、持久化、
    认证、演示模式、事件采集等）真正落地。

    策略由 ``restart_mode`` 决定，并在响应的 ``mode`` 字段回传：

    - ``python-relaunch``（非容器默认）：用 ``sys.executable -m flowerui.relaunch``
      拉起继任者进程（不经过任何 shell / shell 脚本），当前进程优雅退出，
      继任者重新级联解析配置后接管服务；
    - ``exit``（容器 / 编排器托管时默认）：**不创建子进程**，因为容器主进程
      退出即容器终止，子进程会被一并杀掉。只让当前进程优雅退出，交由外部
      重启策略（Docker / k8s / systemd / supervisord）重新拉起；
    - ``disabled``：直接返回 409，不执行任何重启动作。

    Celery worker 属于独立进程，其相关配置需另行重启 worker。
    """
    plan = restart_service.plan_restart()
    if plan["mode"] == restart_service.MODE_DISABLED:
        raise HTTPException(
            status_code=409,
            detail="界面重启已禁用（restart_mode=disabled）：请通过容器编排器或进程管理器重启服务。",
        )

    restart_service.restart_later(plan=plan)
    if plan["mode"] == restart_service.MODE_EXIT:
        message = "后端进程即将优雅退出，将由容器 / 进程管理器重新拉起，请稍候刷新页面"
    else:
        message = "后端正在重启，请稍候刷新页面"
    return RestartResponse(
        message=message,
        mode=str(plan["mode"]),
        container=bool(plan["container"]),
        detail=str(plan["reason"]),
    )

"""配置查看与修改接口。

- ``GET  /api/config``        分组返回全部配置项（敏感值脱敏）
- ``PUT  /api/config``        批量修改，落盘后立即生效
- ``POST /api/config/reload`` 不重启进程，重新应用最新配置来源并分级报告
- ``POST /api/config/reset``  恢复到进程启动时的取值
- ``GET  /api/config/ui``     免鉴权，供前端启动时读取界面默认值
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ...core.security import get_current_user
from ...services import runtime_config

logger = logging.getLogger("flowerui.config_api")
router = APIRouter(prefix="/api/config", tags=["config"])


class UpdateRequest(BaseModel):
    changes: Dict[str, Any] = Field(default_factory=dict)


class ResetRequest(BaseModel):
    keys: Optional[List[str]] = None


@router.get("/ui")
async def get_ui_defaults():
    """界面默认值。刻意不鉴权：登录页本身也需要按服务端默认主题渲染。"""
    return runtime_config.ui_defaults()


@router.get("")
async def get_config(_user: str = Depends(get_current_user)):
    return runtime_config.describe()


@router.put("")
async def update_config(payload: UpdateRequest, _user: str = Depends(get_current_user)):
    try:
        applied = runtime_config.update(payload.changes)
    except runtime_config.ConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "ok": True,
        "applied": applied,
        "message": f"已更新 {len(applied)} 项配置",
        "config": runtime_config.describe(),
    }


@router.post("/reload")
async def reload_config(_user: str = Depends(get_current_user)):
    """不重启进程，重新读取并应用最新配置（app.conf / 环境变量 / CLI / TOML）。

    适用于手工改了配置文件或注入了新环境变量、又不想重启服务的场景。
    响应按「已热生效 / 需重启 / 仅下次启动」分级列出本次变更，前端据此提示。
    """
    try:
        result = runtime_config.hot_reload()
    except Exception as exc:  # noqa: BLE001 - 失败要给出可诊断的回包
        logger.exception("配置热应用失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="配置热应用失败: %s" % exc,
        ) from exc

    parts = []
    if result["applied"]:
        parts.append("已即时生效 %d 项" % len(result["applied"]))
    if result["requires_restart"]:
        parts.append("需重启服务才能生效: %s" % "、".join(result["requires_restart"]))
    if result["next_start_only"]:
        parts.append("仅下次启动生效: %s" % "、".join(result["next_start_only"]))
    result["message"] = "；".join(parts) if parts else "配置无变化，无需重启"
    return result


@router.post("/reset")
async def reset_config(payload: ResetRequest, _user: str = Depends(get_current_user)):
    restored = runtime_config.reset(payload.keys)
    return {
        "ok": True,
        "restored": restored,
        "message": f"已恢复 {len(restored)} 项配置的启动值" if restored else "当前没有被修改过的配置项",
        "config": runtime_config.describe(),
    }


@router.post("/materialize")
async def materialize_config(_user: str = Depends(get_current_user)):
    """生成一份完整带中文注释的默认配置模板（保留手工键）。"""
    return runtime_config.materialize()

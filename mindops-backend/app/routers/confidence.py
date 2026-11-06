"""F-04 回环参数接口（§3.1 查询 / §3.2 调参）。

- `GET  /config/confidence`：任意已登录角色，返回**生效值**（DB 覆盖优先于 env）
- `PUT  /config/confidence`：仅 admin；白名单 4 字段；越界 422；改动仅影响后续新 run
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.agents import loop_config
from app.auth.dependencies import get_current_user
from app.models import User
from app.schemas import ConfidenceConfigOut, ConfidencePatch, ConfidencePatchOut

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/confidence", response_model=ConfidenceConfigOut)
async def get_confidence(user: User = Depends(get_current_user)):
    values = await loop_config.effective_config()
    return ConfidenceConfigOut(
        conf_threshold=values["conf_threshold"],
        max_retry=values["max_retry"],
        expand_recall=loop_config.expand_recall(),
        reasoning_escalation=values["reasoning_escalation"],
        cross_component_fallback=values["cross_component_fallback"],
    )


@router.put("/confidence", response_model=ConfidencePatchOut)
async def put_confidence(body: ConfidencePatch, user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "admin role required"})
    try:
        values = await loop_config.apply_patch(
            body.model_dump(exclude_none=True), updated_by=user.username
        )
    except ValueError as exc:
        # §3.2 响应 422：conf_threshold ∈ (0,1)；max_retry ∈ [0,5]
        # P1-2：detail 统一为结构化信封（原来是裸字符串）
        raise HTTPException(422, detail={
            "code": "INVALID_CONFIDENCE_CONFIG",
            "message": str(exc),
        }) from exc
    return ConfidencePatchOut(
        conf_threshold=values["conf_threshold"],
        max_retry=values["max_retry"],
        updated_by=user.username,
    )

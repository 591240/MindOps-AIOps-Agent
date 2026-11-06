"""全局异常归一化（P1-2：统一错误响应信封）。

问题：修复前错误响应存在三类形态并存——
  ① 业务错误  `{"detail": {"code": ..., "message": ...}}`
  ② Pydantic 422 原始列表 `{"detail": [{"loc": [...], "msg": ...}]}`
  ③ 部分路由直接抛字符串  `{"detail": "incident not found"}`
前端无法用一套逻辑解析。

策略：
  1. 注册 `RequestValidationError` 处理器 → 归一化 ②；
  2. 注册 `StarletteHTTPException` 处理器 → 把 ③ 的字符串 detail 提升为
     `{code, message}`，同时保证 ① 原样透传；
  3. 注册未捕获异常处理器 → 生产不回传 traceback（避免信息泄露）。

注意：成功响应体暂不包裹 `{code,message,data}`——那会一次性破坏所有
已对接的消费者（含 tests 与 docs/5 现存契约），属于独立改造项。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.observability.logging import log_event

logger = logging.getLogger("mindops.errors")

# 常见状态码 → 机器错误码（字符串 detail 无处取 code 时兜底）
_CODE_BY_STATUS = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    423: "LOCKED",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
}


def normalize_detail(status_code: int, detail: object) -> dict:
    """把任意形态的 detail 归一化为 `{code, message[, errors]}`。"""
    if isinstance(detail, dict):
        # 已是结构化信封：确保有 code/message 两个键
        normalized = dict(detail)
        normalized.setdefault("code", _CODE_BY_STATUS.get(status_code, "ERROR"))
        normalized.setdefault("message", "")
        return normalized

    if isinstance(detail, list):
        # Pydantic 校验错误列表
        return {
            "code": "VALIDATION_ERROR",
            "message": "请求参数校验失败",
            "errors": [
                {
                    "loc": list(item.get("loc", [])),
                    "msg": item.get("msg", ""),
                    "type": item.get("type", ""),
                }
                for item in detail
                if isinstance(item, dict)
            ],
        }

    return {
        "code": _CODE_BY_STATUS.get(status_code, "ERROR"),
        "message": str(detail),
    }


def install_exception_handlers(app: FastAPI) -> None:
    """挂载全局异常处理器。"""

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": normalize_detail(422, exc.errors())},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": normalize_detail(exc.status_code, exc.detail)},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        # 记录完整堆栈（服务端可查），但响应体只给机器码
        logger.exception("unhandled error: %s %s", request.method, request.url.path)
        log_event(40, "app.unhandled_error", path=request.url.path, error=type(exc).__name__)
        message = str(exc) if settings.debug else "服务器内部错误"
        return JSONResponse(
            status_code=500,
            content={"detail": {"code": "INTERNAL_ERROR", "message": message}},
        )

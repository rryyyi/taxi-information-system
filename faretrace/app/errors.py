"""统一错误模型：所有接口失败都返回同一结构。

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or type(self).code
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class ValidationError(AppError):
    status_code = 422
    code = "VALIDATION_ERROR"


class StateError(AppError):
    status_code = 409
    code = "INVALID_STATE_TRANSITION"

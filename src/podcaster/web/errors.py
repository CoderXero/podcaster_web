from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def api_error_response(error: APIError, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"error": {
        "code": error.code, "message": error.message, "details": error.details,
        "request_id": request_id or f"req_{uuid.uuid4().hex}",
    }})


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return api_error_response(exc, getattr(request.state, "request_id", None))


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Do not return provider payloads or filesystem details to the browser.
    return api_error_response(APIError(500, "INTERNAL_ERROR", "An unexpected server error occurred."),
                              getattr(request.state, "request_id", None))


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"loc": error.get("loc", []), "msg": str(error.get("msg", "invalid value")),
               "type": error.get("type", "validation_error")} for error in exc.errors()]
    return api_error_response(APIError(422, "VALIDATION_ERROR", "The request did not pass validation.",
                                       {"errors": errors}), getattr(request.state, "request_id", None))

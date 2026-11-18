"""Wire errors for the Gemini Models API, including dependency failures."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from lumen.services.completion_api import CompletionError
from lumen.services.provider_errors import ProviderError

_STATUS = {
    400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
    405: "UNIMPLEMENTED", 409: "FAILED_PRECONDITION", 413: "RESOURCE_EXHAUSTED", 429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL", 501: "UNIMPLEMENTED", 502: "UNAVAILABLE", 503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED",
}


def is_google_model_path(path: str) -> bool:
    return path == "/v1beta/models" or path.startswith("/v1beta/models/")


def google_error(code: int, message: str) -> dict:
    code = 400 if code == 422 else code
    return {"error": {"code": code, "message": message, "status": _STATUS.get(code, "UNKNOWN")}}


def google_error_response(code: int, message: str, headers: dict | None = None) -> JSONResponse:
    payload = google_error(code, message)
    return JSONResponse(payload, status_code=payload["error"]["code"], headers={"Cache-Control": "no-store", **(headers or {})})


class GoogleModelRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await handle(request)
            except RequestValidationError as exc:
                # Name fields without echoing the caller's contents or media bytes.
                fields = dict.fromkeys(".".join(map(str, error["loc"])) for error in exc.errors())
                return google_error_response(400, f"Invalid request: {', '.join(fields)}")
            except HTTPException as exc:
                message = exc.detail if isinstance(exc.detail, str) else "Request rejected"
                return google_error_response(exc.status_code, message, exc.headers)
            except CompletionError as exc:
                return google_error_response(exc.status_code, exc.message)
            except ProviderError as exc:
                # A rejected provider credential is not a rejected Lumen API key.
                status = exc.status_code if exc.status_code in {400, 422, 429, 504} else 502
                return google_error_response(status, exc.code)

        return handler

"""Sanitized provider failures shared by transport and durable outcome classification.

A received rejection is distinct from an ambiguous transport failure. No exception
retains an upstream request, response, URL, headers, or error body.
"""

from __future__ import annotations


class ProviderError(Exception):
    status_code = 502
    known_rejection = False
    code = "provider_error"

    def __init__(
        self, message: str | None = None, model: str | None = None,
        llm_provider: str | None = None, *, status_code: int | None = None, **_: object,
    ) -> None:
        # Caller/upstream strings may contain credentials. Only our class-owned code
        # is allowed into exception text, even when emulating an older constructor.
        del message, model, llm_provider
        if status_code is not None:
            self.status_code = status_code
        self.message = self.code
        super().__init__(self.code)


class BadRequestError(ProviderError):
    status_code = 400
    known_rejection = True
    code = "provider_request_rejected"


class AuthenticationError(ProviderError):
    status_code = 401
    known_rejection = True
    code = "provider_authentication_rejected"


class PermissionDeniedError(ProviderError):
    status_code = 403
    known_rejection = True
    code = "provider_permission_denied"


class NotFoundError(ProviderError):
    status_code = 404
    known_rejection = True
    code = "provider_not_found"


class UnprocessableEntityError(ProviderError):
    status_code = 422
    known_rejection = True
    code = "provider_request_unprocessable"


class RateLimitError(ProviderError):
    status_code = 429
    known_rejection = True
    code = "provider_rate_limited"


class APIConnectionError(ProviderError):
    code = "provider_transport_outcome_unknown"


class Timeout(APIConnectionError):
    status_code = 504
    code = "provider_timeout_outcome_unknown"


class InternalServerError(ProviderError):
    code = "provider_upstream_outcome_unknown"


def error_for_status(status_code: int) -> ProviderError:
    error_type = {
        400: BadRequestError, 401: AuthenticationError, 403: PermissionDeniedError,
        404: NotFoundError, 413: BadRequestError, 415: BadRequestError,
        422: UnprocessableEntityError, 429: RateLimitError,
    }.get(status_code, InternalServerError)
    return error_type(status_code=status_code)

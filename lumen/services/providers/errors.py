"""Provider-domain exceptions."""


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class ProviderNotFoundError(LookupError):
    """프로바이더/모델 미존재 — 404."""


class ModelsDevImportConflictError(RuntimeError):
    """models.dev provider mapping would orphan imported local prices."""


class ProviderValidationError(ValueError):
    """입력 검증 실패/제약 위반 — 400."""


class AmbiguousModelRouteError(RuntimeError):
    """More than one active route exposes the requested public model/provider pair."""


class ProviderConfigurationChangedError(RuntimeError):
    """Active execution route changed before its durable snapshot was committed."""


class ActiveRunConfigurationConflict(RuntimeError):
    """An admin mutation would alter a nonterminal run's executor route."""


class ProviderAuthAttemptConflict(RuntimeError):
    """Another administrator owns the active provider-auth attempt."""


_SUBSCRIPTION_MESSAGES = {
    "subscription_auth_required": "구독 인증이 필요합니다",
    "subscription_upstream_unavailable": "구독 인증 공급자에 연결할 수 없습니다",
    "subscription_rate_limited": "구독 인증 요청이 제한되었습니다",
    "subscription_auth_invalid_response": "구독 인증 공급자의 응답을 확인할 수 없습니다",
}


class ProviderSubscriptionError(RuntimeError):
    """Safe subscription failure with a stable client-facing code and status."""

    def __init__(self, code: str, status_code: int):
        if code not in _SUBSCRIPTION_MESSAGES:
            raise ValueError("unknown provider subscription error code")
        self.code = code
        self.status_code = status_code
        self.message = _SUBSCRIPTION_MESSAGES[code]
        super().__init__(self.message)

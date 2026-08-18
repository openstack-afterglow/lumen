"""Provider-domain exceptions."""


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class ProviderNotFoundError(LookupError):
    """프로바이더/모델 미존재 — 404."""


class ModelsDevImportConflictError(RuntimeError):
    """models.dev provider mapping would orphan imported local prices."""


class ProviderValidationError(ValueError):
    """입력 검증 실패/제약 위반 — 400."""


class ProviderConfigurationChangedError(RuntimeError):
    """Active execution route changed before its durable snapshot was committed."""


class ActiveRunConfigurationConflict(RuntimeError):
    """An admin mutation would alter a nonterminal run's executor route."""

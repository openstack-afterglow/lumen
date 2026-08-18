"""Durable run domain errors."""


class DurableRunError(RuntimeError):
    pass


class DurableRunProtocolMismatch(DurableRunError):
    """A queued payload cannot safely be interpreted by this worker."""


class DurableRunInputError(DurableRunError):
    pass


class DurableRunLeaseLost(DurableRunError):
    """The worker must stop before a stale lease holder writes another event."""


class DurableRunProviderResultUnknown(DurableRunError):
    """An external provider call may have completed, but no result is durable."""

    pass


class DurableRunConflict(DurableRunError):
    pass


class DurableRunNotFound(DurableRunError):
    pass


class DurableRunCursorExpired(DurableRunError):
    pass

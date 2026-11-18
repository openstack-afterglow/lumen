"""Billing payer identity."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Payer:
    scope: Literal["user", "domain"]
    user_id: str | None
    domain_id: str | None

    def __post_init__(self) -> None:
        if not ((self.scope == "user" and self.user_id and self.domain_id is None)
                or (self.scope == "domain" and self.domain_id and self.user_id is None)):
            raise ValueError("invalid billing payer")

    def columns(self) -> dict[str, str | None]:
        return {"payer_user_id": self.user_id, "payer_domain_id": self.domain_id}

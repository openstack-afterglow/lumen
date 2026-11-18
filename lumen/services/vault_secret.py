"""Credential-shape refusals matching Agent Vault's shared secret detector."""

import re

_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b", re.ASCII),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", re.ASCII),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b", re.ASCII),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b", re.ASCII),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", re.ASCII),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b", re.ASCII),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", re.ASCII),
    re.compile(
        r"""\b(?:api[_-]?key|secret|token|passwd|password)\b["']?\s*[:=]\s*["']?[A-Za-z0-9/+_.-]{20,}""",
        re.ASCII | re.IGNORECASE,
    ),
)


def contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _PATTERNS)

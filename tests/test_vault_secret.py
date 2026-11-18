"""Parity checks for the credential shapes accepted by Agent Vault's detector."""

import pytest

from lumen.services.vault_secret import contains_secret


@pytest.mark.parametrize(("match", "miss"), [
    ("-----BEGIN EC PRIVATE KEY-----", "-----BEGIN EC PUBLIC KEY-----"),
    ("AKIA" + "A" * 16, "AKIA" + "A" * 15),
    ("ghp_" + "A" * 36, "ghp_" + "A" * 35),
    ("github_pat_" + "A" * 22, "github_pat_" + "A" * 21),
    ("sk-" + "A" * 20, "sk-" + "A" * 19),
    ("xoxb-" + "A" * 10, "xoxb-" + "A" * 9),
    ("AIza" + "A" * 35, "AIza" + "A" * 34),
    ("eyJ" + ".".join(["A" * 10] * 3), "eyJ" + "/".join(["A" * 10] * 3)),
    ('"API-Key" : "' + "A" * 20, '"API-Key" : "' + "A" * 19),
])
def test_credential_shape_and_near_miss(match, miss):
    assert contains_secret(match)
    assert not contains_secret(miss)


@pytest.mark.parametrize("space", ["\u00a0", "\u2007", "\u2028", "\u2029", "\u3000", "\ufeff"])
def test_credential_assignment_accepts_ecmascript_whitespace(space):
    assert contains_secret(f"token{space}={space}" + "A" * 20)


def test_credential_assignment_does_not_treat_nel_as_ecmascript_whitespace():
    assert not contains_secret("token\u0085= " + "A" * 20)

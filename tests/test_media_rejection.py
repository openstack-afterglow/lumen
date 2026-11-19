from __future__ import annotations

import pytest

from lumen.services.durable_runs.media import rejection_http_response, rejection_message


@pytest.mark.parametrize("upstream, expected", [
    (400, (400, "invalid_request_error")),
    (413, (400, "invalid_request_error")),
    (415, (400, "invalid_request_error")),
    (422, (400, "invalid_request_error")),
    (429, (429, "rate_limit_error")),
    (401, (502, "api_error")),
    (403, (502, "api_error")),
    (404, (502, "api_error")),
])
def test_refusal_message_round_trips_to_the_caller_status_class(upstream, expected):
    message = rejection_message(upstream)
    status, error_type, text = rejection_http_response(message)
    assert (status, error_type) == expected and text == message


def test_a_message_without_an_upstream_status_is_a_bad_gateway():
    # Events written before the status was carried never claim a caller error.
    assert rejection_http_response("upstream provider rejected the request")[:2] == (502, "api_error")
    assert rejection_http_response(None)[:2] == (502, "api_error")

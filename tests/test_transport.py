from unittest.mock import patch, MagicMock

import pytest

from klaviyo_cli.transport import (
    APIError, AuthError, DirectTransport, ensure_delete_allowed,
)


def test_delete_template_allowed():
    ensure_delete_allowed("/api/templates/AbC123/")  # no raise


def test_delete_campaign_blocked():
    with pytest.raises(AuthError, match="not allowed"):
        ensure_delete_allowed("/api/campaigns/AbC123/")


@patch("klaviyo_cli.transport.requests.get")
def test_direct_get_sends_auth_headers(mock_get):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": []}
    mock_get.return_value = resp
    t = DirectTransport("pk_test")
    out = t.call("GET", "/api/campaigns/")
    assert out == {"data": []}
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Klaviyo-API-Key pk_test"
    assert headers["revision"] == "2025-07-15"


@patch("klaviyo_cli.transport.requests.get")
def test_klaviyo_error_extracted(mock_get):
    resp = MagicMock(status_code=400)
    resp.json.return_value = {"errors": [{"detail": "Invalid filter"}]}
    mock_get.return_value = resp
    with pytest.raises(APIError, match="Invalid filter"):
        DirectTransport("pk_test").call("GET", "/api/campaigns/")


def test_direct_delete_blocked_before_http():
    with pytest.raises(AuthError):
        DirectTransport("pk_test").call("DELETE", "/api/lists/X/")

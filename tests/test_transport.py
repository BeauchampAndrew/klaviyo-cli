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


@patch("klaviyo_cli.transport.requests.post")
def test_direct_upload_sends_multipart_with_auth(mock_post):
    """File uploads (POST /api/image-upload/) are multipart, not JSON: requests must
    build the boundary itself, so no JSON Content-Type header may be forced."""
    resp = MagicMock(status_code=201)
    resp.json.return_value = {"data": {"id": "IMG1"}}
    mock_post.return_value = resp
    files = {"file": ("hero.png", b"\x89PNG", "image/png")}
    out = DirectTransport("pk_test").upload("/api/image-upload/", files=files, data={"name": "hero"})
    assert out == {"data": {"id": "IMG1"}}
    assert mock_post.call_args.args[0] == "https://a.klaviyo.com/api/image-upload/"
    kwargs = mock_post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Klaviyo-API-Key pk_test"
    assert "revision" in kwargs["headers"]
    assert "Content-Type" not in kwargs["headers"]
    assert kwargs["files"] == files
    assert kwargs["data"] == {"name": "hero"}


@patch("klaviyo_cli.transport.requests.post")
def test_direct_upload_error_extracted(mock_post):
    resp = MagicMock(status_code=400)
    resp.json.return_value = {"errors": [{"detail": "Unsupported image format"}]}
    mock_post.return_value = resp
    with pytest.raises(APIError, match="Unsupported image format"):
        DirectTransport("pk_test").upload("/api/image-upload/", files={"file": ("x", b"", "a/b")})


@patch("klaviyo_cli.transport.time.sleep")
@patch("klaviyo_cli.transport.requests.get")
def test_direct_call_retries_429_honoring_retry_after(mock_get, mock_sleep):
    limited = MagicMock(status_code=429, headers={"Retry-After": "2"})
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"data": "ok"}
    mock_get.side_effect = [limited, ok]
    assert DirectTransport("pk_test").call("GET", "/api/segments/") == {"data": "ok"}
    mock_sleep.assert_called_once_with(3)


@patch("klaviyo_cli.transport.time.sleep")
@patch("klaviyo_cli.transport.requests.post")
def test_direct_upload_retries_429(mock_post, mock_sleep):
    limited = MagicMock(status_code=429, headers={"Retry-After": "1"})
    ok = MagicMock(status_code=201)
    ok.json.return_value = {"data": {"id": "IMG1"}}
    mock_post.side_effect = [limited, ok]
    out = DirectTransport("pk_test").upload("/api/image-upload/", files={"file": ("x", b"", "image/png")})
    assert out == {"data": {"id": "IMG1"}}
    assert mock_post.call_count == 2

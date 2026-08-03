from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import build_context, main


def test_help_needs_no_auth(monkeypatch):
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Unofficial Klaviyo CLI" in result.output


def test_build_context_is_lazy(monkeypatch):
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    ctx = build_context(None)  # must NOT raise despite missing creds
    assert ctx["label"] == "default"


@patch("klaviyo_cli.cli.resolve_transport")
def test_lazy_call_resolves_once(mock_resolve):
    mock_resolve.return_value.call.return_value = {"data": []}
    ctx = build_context("acme")
    assert ctx["label"] == "acme"
    ctx["call"]("GET", "/api/campaigns/")
    ctx["call"]("GET", "/api/campaigns/")
    assert mock_resolve.call_count == 1

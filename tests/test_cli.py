from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from klaviyo_cli.cli import build_context, entry, main


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


@patch("klaviyo_cli.cli.entry_points")
@patch("klaviyo_cli.cli.main")
def test_entry_no_host_falls_back_to_main(mock_main, mock_entry_points):
    """When no host entry points are registered, entry() calls main()."""
    mock_entry_points.return_value = []
    entry()
    mock_main.assert_called_once()


@patch("klaviyo_cli.cli.entry_points")
@patch("klaviyo_cli.cli.main")
def test_entry_host_override_calls_host_group(mock_main, mock_entry_points):
    """When a host entry point returns a group, entry() calls that group instead of main()."""
    mock_group = MagicMock()
    mock_ep = MagicMock()
    mock_ep.load.return_value.return_value = mock_group
    mock_entry_points.return_value = [mock_ep]

    entry()

    # Verify the host entry point was loaded and called as a factory
    mock_ep.load.assert_called_once()
    # Verify the host group was called
    mock_group.assert_called_once()
    # Verify main was NOT called
    mock_main.assert_not_called()

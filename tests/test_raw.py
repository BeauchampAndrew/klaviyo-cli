from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main


@patch("klaviyo_cli.cli.build_context")
def test_api_passthrough_get(mock_build):
    seen = []

    def call(method, path, body=None, revision=None):
        seen.append((method, path, body))
        return {"data": {"id": "X"}}

    mock_build.return_value = {"call": call, "label": "t"}
    result = CliRunner().invoke(main, ["api", "GET", "/api/campaigns/X/"])
    assert result.exit_code == 0, result.output
    assert seen == [("GET", "/api/campaigns/X/", None)]


@patch("klaviyo_cli.cli.build_context")
def test_api_passthrough_delete_blocked(mock_build):
    from klaviyo_cli.transport import DirectTransport
    mock_build.return_value = {
        "call": DirectTransport("pk_x").call, "label": "t"}
    result = CliRunner().invoke(main, ["api", "DELETE", "/api/segments/X/"])
    assert result.exit_code != 0
    assert "not allowed" in result.output

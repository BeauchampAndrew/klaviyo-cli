from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli._util import _parse_send_time
from klaviyo_cli.cli import main


def _fake_ctx_factory(response):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body))
        return response

    return {"call": call, "label": "test-account"}, calls


@patch("klaviyo_cli.cli.build_context")
def test_upload_sms_builds_campaign_payload(mock_build):
    """upload-sms must POST /api/campaigns/ with the parsed ISO send time
    and the include/exclude audience lists in the request body."""
    response = {
        "data": {
            "id": "CAMP1",
            "attributes": {"name": "SMS [04-11-2026] Heavy Flow"},
        }
    }
    ctx_obj, calls = _fake_ctx_factory(response)
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "upload-sms",
        "--name", "SMS [04-11-2026] Heavy Flow",
        "--body", "Flash sale today only!",
        "--date", "04-11-2026",
        "--time", "3:00 PM EDT",
        "--include", "SEGA,SEGB",
        "--exclude", "SEGC",
    ])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/api/campaigns/"

    attrs = body["data"]["attributes"]
    assert attrs["send_strategy"]["datetime"] == _parse_send_time("04-11-2026", "3:00 PM EDT")
    assert attrs["audiences"]["included"] == ["SEGA", "SEGB"]
    assert attrs["audiences"]["excluded"] == ["SEGC"]
    message = attrs["campaign-messages"]["data"][0]
    assert message["attributes"]["definition"]["content"]["body"] == "Flash sale today only!"
    assert "CAMP1" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_upload_sms_body_from_file(mock_build, tmp_path):
    """--body @path/to/file.txt must read the SMS body text from disk."""
    body_file = tmp_path / "sms_body.txt"
    body_file.write_text("Text from file body\n")

    response = {"data": {"id": "CAMP2", "attributes": {"name": "File SMS"}}}
    ctx_obj, calls = _fake_ctx_factory(response)
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "upload-sms",
        "--name", "File SMS",
        "--body", f"@{body_file}",
        "--date", "04-11-2026",
        "--time", "3:00 PM EDT",
        "--include", "SEGA",
    ])
    assert result.exit_code == 0, result.output
    _, _, body = calls[0]
    message = body["data"]["attributes"]["campaign-messages"]["data"][0]
    assert message["attributes"]["definition"]["content"]["body"] == "Text from file body"

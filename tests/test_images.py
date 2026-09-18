from unittest.mock import patch

import pytest
from click.testing import CliRunner

from klaviyo_cli.cli import build_context, main
from klaviyo_cli.transport import AuthError


def _upload_ctx(response):
    uploads = []

    def upload(path, files, data=None, revision=None):
        uploads.append((path, files, data))
        return response

    return {"call": None, "upload": upload, "label": "test-account"}, uploads


RESP = {"data": {"id": "100000001", "attributes": {
    "name": "hero", "image_url": "https://cdn.example.com/images/abc.png"}}}


@patch("klaviyo_cli.cli.build_context")
def test_upload_image_posts_file_and_prints_url(mock_build, tmp_path):
    img = tmp_path / "spring-hero.png"
    img.write_bytes(b"\x89PNG-bytes")
    ctx_obj, uploads = _upload_ctx(RESP)
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["upload-image", str(img), "--name", "Spring hero"])
    assert result.exit_code == 0, result.output
    path, files, data = uploads[0]
    assert path == "/api/image-upload/"
    filename, content, mimetype = files["file"]
    assert filename == "spring-hero.png"
    assert content == b"\x89PNG-bytes"
    assert mimetype == "image/png"
    assert data["name"] == "Spring hero"
    assert "https://cdn.example.com/images/abc.png" in result.output
    assert "100000001" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_upload_image_name_defaults_to_filename(mock_build, tmp_path):
    img = tmp_path / "hero.jpg"
    img.write_bytes(b"jpg")
    ctx_obj, uploads = _upload_ctx(RESP)
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["upload-image", str(img)])
    assert result.exit_code == 0, result.output
    assert uploads[0][2]["name"] == "hero.jpg"
    assert uploads[0][1]["file"][2] == "image/jpeg"


def test_build_context_upload_rejects_json_only_transport():
    class JsonOnly:
        def call(self, method, path, body=None, revision=None):
            return {}

    with patch("klaviyo_cli.cli.resolve_transport", return_value=JsonOnly()):
        ctx = build_context(None)
        with pytest.raises(AuthError, match="upload"):
            ctx["upload"]("/api/image-upload/", files={})

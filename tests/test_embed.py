from click.testing import CliRunner

from klaviyo_cli.embed import build_host_group, wrap_with_account
from klaviyo_cli.cli import main


class FakeTransport:
    def __init__(self, account):
        self.account = account
        self.calls = []

    def call(self, method, path, body=None, revision=None):
        self.calls.append((method, path))
        return {"data": [], "links": {}}


def test_wrapped_command_takes_account_positional():
    transports = {}

    def resolver(account):
        transports[account] = FakeTransport(account)
        return transports[account]

    group = build_host_group(resolver)
    result = CliRunner().invoke(group, ["list-campaigns", "acme-co"])
    assert result.exit_code == 0, result.output
    assert "acme-co" in result.output          # label flows into display
    assert transports["acme-co"].calls          # resolver's transport was used


def test_wrapped_help_needs_no_auth():
    def resolver(account):
        raise RuntimeError("must not resolve for --help")

    group = build_host_group(resolver)
    result = CliRunner().invoke(group, ["list-campaigns", "--help"])
    assert result.exit_code == 0
    assert "ACCOUNT" in result.output


class FakeCampaignTransport:
    """Returns a plausible get-campaign payload regardless of the exact path,
    and records every call made against it."""

    def __init__(self, account):
        self.account = account
        self.calls = []

    def call(self, method, path, body=None, revision=None):
        self.calls.append((method, path))
        return {
            "data": {
                "id": "CAMP1",
                "attributes": {
                    "name": "Test Campaign",
                    "status": "Draft",
                    "send_time": None,
                    "created_at": "2026-01-01T00:00:00Z",
                    "audiences": {"included": ["SEG1"], "excluded": []},
                    "send_strategy": {"method": "static"},
                },
            },
            "included": [],
        }


def test_wrapped_get_campaign_binds_positional_after_account():
    """get-campaign has a required CAMPAIGN_ID positional. The wrapped command
    must accept ACCOUNT first (['get-campaign', 'acme', 'CAMP1']) and still
    bind CAMPAIGN_ID correctly to the original callback."""
    transports = {}

    def resolver(account):
        transports[account] = FakeCampaignTransport(account)
        return transports[account]

    group = build_host_group(resolver)
    result = CliRunner().invoke(group, ["get-campaign", "acme-co", "CAMP1"])
    assert result.exit_code == 0, result.output
    assert "Test Campaign" in result.output
    assert transports["acme-co"].calls
    # The campaign_id positional must have made it into the request path.
    assert any("CAMP1" in path for _method, path in transports["acme-co"].calls)


def test_wrap_with_account_pops_dashed_arg_name():
    """Regression: click normalizes a param decl like "client-id" into the
    dest name "client_id" when building callback kwargs. wrap_with_account
    must pop by that normalized name, not the raw arg_name, or every
    invocation KeyErrors."""
    transports = {}

    def resolver(account):
        transports[account] = FakeTransport(account)
        return transports[account]

    cmd = wrap_with_account(main.commands["list-campaigns"], resolver, arg_name="client-id")
    # Standalone invocation (no host group ahead of it) starts with no
    # ctx.obj at all; seed the "json" key the same way build_host_group's
    # group callback normally would, so this test isolates the pop-by-name
    # regression rather than the (separately-tested) obj-merging behavior.
    result = CliRunner().invoke(cmd, ["acme-co"], obj={"json": False})
    assert result.exit_code == 0, result.output
    assert "acme-co" in result.output
    assert transports["acme-co"].calls

    # --help must also work and show the normalized-but-uppercased metavar.
    help_result = CliRunner().invoke(cmd, ["--help"])
    assert help_result.exit_code == 0
    assert "CLIENT_ID" in help_result.output


class FakeUploadTransport(FakeTransport):
    def __init__(self, account):
        super().__init__(account)
        self.uploads = []

    def upload(self, path, files, data=None, revision=None):
        self.uploads.append((path, files, data))
        return {"data": {"id": "IMG1", "attributes": {"image_url": "https://cdn.example.com/x.png"}}}


def test_wrapped_upload_image_uses_transport_upload(tmp_path):
    img = tmp_path / "hero.png"
    img.write_bytes(b"\x89PNG")
    transports = {}

    def resolver(account):
        transports[account] = FakeUploadTransport(account)
        return transports[account]

    result = CliRunner().invoke(build_host_group(resolver), ["upload-image", "acme-co", str(img)])
    assert result.exit_code == 0, result.output
    assert transports["acme-co"].uploads[0][0] == "/api/image-upload/"
    assert "https://cdn.example.com/x.png" in result.output


def test_wrapped_upload_on_json_only_transport_is_a_clean_error(tmp_path):
    """A host transport without upload() (e.g. a JSON-only API proxy) must produce a
    readable error, not an AttributeError traceback."""
    img = tmp_path / "hero.png"
    img.write_bytes(b"\x89PNG")
    result = CliRunner().invoke(build_host_group(FakeTransport), ["upload-image", "acme-co", str(img)])
    assert result.exit_code != 0
    assert "No such command" not in result.output
    assert "Traceback" not in result.output
    assert "upload" in result.output.lower()

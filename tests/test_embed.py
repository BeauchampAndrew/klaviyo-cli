from click.testing import CliRunner

from klaviyo_cli.embed import build_host_group


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

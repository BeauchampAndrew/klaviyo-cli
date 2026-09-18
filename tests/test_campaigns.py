from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli._util import _parse_send_time
from klaviyo_cli.cli import main


def test_parse_send_time_est():
    assert _parse_send_time("04-11-2026", "3:00 PM EST") == "2026-04-11T15:00:00-04:00"


def test_parse_send_time_24h_cst():
    assert _parse_send_time("2026-04-11", "15:00 CST") == "2026-04-11T15:00:00-05:00"


def _fake_ctx_factory(pages):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body))
        return pages.pop(0)

    return {"call": call, "label": "test-account"}, calls


@patch("klaviyo_cli.cli.build_context")
def test_list_campaigns_paginates_and_prints(mock_build):
    page1 = {
        "data": [{"id": "C1", "attributes": {"name": "July Promo", "status": "Sent",
                                             "send_time": "2026-07-01T10:00:00Z"}}],
        "links": {"next": "https://a.klaviyo.com/api/campaigns/?page%5Bcursor%5D=N"},
    }
    page2 = {"data": [], "links": {}}
    ctx_obj, calls = _fake_ctx_factory([page1, page2])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["list-campaigns"])
    assert result.exit_code == 0, result.output
    assert "July Promo" in result.output
    assert "test-account" in result.output
    assert calls[0][0] == "GET"


@patch("klaviyo_cli.cli.build_context")
def test_list_campaigns_builds_status_filter(mock_build):
    """list-campaigns must encode --status/--channel into the filter query."""
    page = {
        "data": [{"id": "C1", "attributes": {"name": "A Campaign", "status": "Sent",
                                             "send_time": "2026-06-04T14:00:00+00:00"}}],
        "links": {"next": None},
    }
    ctx_obj, calls = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["list-campaigns", "--status", "Sent", "--channel", "email"])
    assert result.exit_code == 0, result.output
    assert "C1" in result.output
    assert "A Campaign" in result.output
    # The request path must encode the status filter.
    path = calls[0][1]
    assert "status" in path and "Sent" in path


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_surfaces_subject_and_preview(mock_build):
    """get-campaign must show the draft's real subject + preview in one call so
    the scheduling workflow can catch a clone-leftover subject line."""
    page = {
        "data": {
            "id": "CAMP1",
            "attributes": {
                "name": "[07-06] Monthly newsletter",
                "status": "Draft",
                "send_time": None,
                "audiences": {"included": ["SEGA"], "excluded": []},
                "send_strategy": {"method": "static"},
            },
        },
        "included": [{
            "type": "campaign-message",
            "id": "MSG1",
            "attributes": {"definition": {"content": {
                "subject": "Seen on you",
                "preview_text": "Now available in Black.",
            }}},
        }],
    }
    ctx_obj, calls = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "CAMP1"])
    assert result.exit_code == 0, result.output
    assert "Subject: Seen on you" in result.output
    assert "Preview: Now available in Black." in result.output
    # Subject must come from the same request via include, not a second hop.
    assert len(calls) == 1
    path = calls[0][1]
    assert "include=campaign-messages" in path


@patch("klaviyo_cli.cli.build_context")
def test_get_creative_extracts_text_and_greps(mock_build):
    msgs = {
        "data": [{
            "id": "MSG1",
            "attributes": {"channel": "email", "definition": {"content": {"subject": "15% off"}}},
            "relationships": {"template": {"data": {"id": "TPL1"}}},
        }]
    }
    template = {"data": {"attributes": {"text": "Intro line\nUse code FATHER26 at checkout\nFooter"}}}
    ctx_obj, calls = _fake_ctx_factory([msgs, template])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-creative", "CAMP1", "--grep", "code"])
    assert result.exit_code == 0, result.output
    assert "FATHER26" in result.output
    # --grep must drop non-matching lines.
    assert "Footer" not in result.output


def _routed_ctx(routes):
    """Fake ctx whose responses are chosen by path prefix. A route value that is
    an Exception instance is raised instead of returned (e.g. a 404 APIError)."""
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body))
        for prefix, resp in routes:
            if path.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected call {method} {path}")

    return {"call": call, "label": "test-account"}, calls


def _campaign_page(send_time=None, strategy=None, included=("SEGA",), excluded=()):
    return {
        "data": {
            "id": "CAMP1",
            "attributes": {
                "name": "[09-20-2026] Trial Offer",
                "status": "Draft" if not send_time else "Scheduled",
                "send_time": send_time,
                "audiences": {"included": list(included), "excluded": list(excluded)},
                "send_strategy": strategy or {"method": "static"},
            },
        },
        "included": [],
    }


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_shows_planned_send_time_on_unscheduled_draft(mock_build):
    """A draft with a date set via patch-campaign keeps it in send_strategy.datetime
    while send_time stays null until scheduled. Printing 'not scheduled' hid a real,
    correct date and sent us chasing a non-problem."""
    page = _campaign_page(strategy={"method": "static", "datetime": "2026-09-20T14:00:00+00:00"})
    ctx_obj, _ = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "CAMP1"])
    assert result.exit_code == 0, result.output
    assert "2026-09-20T14:00:00+00:00" in result.output
    assert "planned" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_without_any_date_still_says_not_scheduled(mock_build):
    ctx_obj, _ = _fake_ctx_factory([_campaign_page()])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "CAMP1"])
    assert result.exit_code == 0, result.output
    assert "Send Time: not scheduled" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_names_flag_resolves_segment_and_list_names(mock_build):
    """--names prints each audience ID with its name. IDs can be segments or lists;
    a 404 on the segment lookup falls through to the list lookup."""
    from klaviyo_cli.transport import APIError
    ctx_obj, calls = _routed_ctx([
        ("/api/campaigns/CAMP1/", _campaign_page(included=("SEGA", "LIST1"), excluded=("SEGB",))),
        ("/api/segments/SEGA/", {"data": {"attributes": {"name": "Engaged 0-30d"}}}),
        ("/api/segments/SEGB/", {"data": {"attributes": {"name": "Standard Unengaged"}}}),
        ("/api/segments/LIST1/", APIError("HTTP 404")),
        ("/api/lists/LIST1/", {"data": {"attributes": {"name": "SEED LIST"}}}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "CAMP1", "--names"])
    assert result.exit_code == 0, result.output
    assert "SEGA (Engaged 0-30d)" in result.output
    assert "LIST1 (SEED LIST)" in result.output
    assert "SEGB (Standard Unengaged)" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_names_flag_keeps_bare_id_when_lookup_fails(mock_build):
    from klaviyo_cli.transport import APIError
    ctx_obj, _ = _routed_ctx([
        ("/api/campaigns/CAMP1/", _campaign_page(included=("GONE1",))),
        ("/api/segments/GONE1/", APIError("HTTP 404")),
        ("/api/lists/GONE1/", APIError("HTTP 404")),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "CAMP1", "--names"])
    assert result.exit_code == 0, result.output
    assert "Include: GONE1" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_patch_campaign_reports_planned_send_time(mock_build):
    """patch-campaign used to print 'Send Time: not set' right after setting a date,
    because the date lives in send_strategy until the campaign is scheduled."""
    resp = {"data": {"id": "CAMP1", "attributes": {
        "name": "[09-20-2026] Trial Offer", "send_time": None,
        "send_strategy": {"method": "static", "datetime": "2026-09-20T14:00:00+00:00"}}}}
    ctx_obj, _ = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-campaign", "CAMP1", "--date", "09-20-2026",
                                       "--time", "10:00 AM EDT"])
    assert result.exit_code == 0, result.output
    assert "not set" not in result.output
    assert "2026-09-20T14:00:00+00:00" in result.output

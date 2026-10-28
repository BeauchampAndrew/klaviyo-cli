from unittest.mock import patch

import pytest
from click.testing import CliRunner

from klaviyo_cli.cli import main
from klaviyo_cli.transport import AuthError, confirmed_delete, ensure_delete_allowed


def _ctx(flow_def=None, audiences=None):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path))
        if method == "DELETE":
            ensure_delete_allowed(path)
            return {"_raw": ""}
        if path.startswith("/api/segments/SEG1/"):
            return {"data": {"attributes": {"name": "Old Seg", "profile_count": 12}}}
        if path.startswith("/api/flows/?"):
            return {"data": [{"id": "FL1", "attributes": {"name": "Push", "status": "live"}}]}
        if path.startswith("/api/flows/FL1/"):
            return {"data": {"attributes": {"definition": flow_def or {}}}}
        if path.startswith("/api/campaign-messages/?"):
            return {"data": [{"id": "C1", "attributes": {"definition": {"name": "Promo", "status": "draft"}},
                              "relationships": {"campaign-audience": {"data": {"id": "AUD1"}}}}]}
        if path.startswith("/api/campaign-audiences/AUD1/"):
            return {"data": {"id": "AUD1", "attributes": {"definition": audiences or {}}}}
        raise AssertionError(path)

    return {"call": call, "label": "t", "json": False}, calls


def test_raw_delete_of_segment_still_blocked():
    with pytest.raises(AuthError, match="klaviyo delete"):
        ensure_delete_allowed("/api/segments/SEG1/")


def test_confirmed_delete_only_unlocks_that_path():
    with confirmed_delete("/api/segments/SEG1/"):
        ensure_delete_allowed("/api/segments/SEG1/")
        with pytest.raises(AuthError):
            ensure_delete_allowed("/api/segments/OTHER/")
    with pytest.raises(AuthError):
        ensure_delete_allowed("/api/segments/SEG1/")


@patch("klaviyo_cli.cli.build_context")
def test_delete_without_confirm_does_nothing(mock_build):
    ctx, calls = _ctx()
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "segment", "SEG1"])
    assert result.exit_code != 0
    assert "--confirm SEG1" in result.output
    assert not any(m == "DELETE" for m, _ in calls)


@patch("klaviyo_cli.cli.build_context")
def test_delete_wrong_confirm_does_nothing(mock_build):
    ctx, calls = _ctx()
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "segment", "SEG1", "--confirm", "SEG2"])
    assert result.exit_code != 0
    assert not any(m == "DELETE" for m, _ in calls)


@patch("klaviyo_cli.cli.build_context")
def test_delete_confirmed_runs_and_warns_about_flow(mock_build):
    ctx, calls = _ctx(flow_def={"triggers": [{"type": "segment", "id": "SEG1"}]})
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "segment", "SEG1", "--confirm", "SEG1"])
    assert result.exit_code == 0, result.output
    assert "flow [FL1] Push (live)" in result.output
    assert ("DELETE", "/api/segments/SEG1/") in calls


@patch("klaviyo_cli.cli.build_context")
def test_delete_segment_lists_unsent_campaigns_using_it(mock_build):
    ctx, _ = _ctx(audiences={"included": ["SEG1"], "excluded": []})
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "segment", "SEG1"])
    assert "campaign [C1] Promo (Draft)" in result.output


def _campaign_ctx(siblings):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path))
        if method == "DELETE":
            ensure_delete_allowed(path)
            return {"_raw": ""}
        if path.startswith("/api/campaign-messages/MSG1/"):
            return {"data": {"id": "MSG1", "attributes": {"definition": {"name": "Promo", "status": "sent"}},
                             "relationships": {"campaign": {"data": {"id": "CONT1"}}}}}
        if path.startswith("/api/campaigns/CONT1/campaign-messages/"):
            return {"data": [{"id": s} for s in siblings]}
        raise AssertionError(path)

    return {"call": call, "label": "t", "json": False}, calls


@patch("klaviyo_cli.cli.build_context")
def test_delete_campaign_removes_its_single_message_container(mock_build):
    """The ID people pass is the message ID; deleting it means deleting the
    container campaign when the message is all it holds."""
    ctx, calls = _campaign_ctx(["MSG1"])
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "campaign", "MSG1", "--confirm", "MSG1"])
    assert result.exit_code == 0, result.output
    assert ("DELETE", "/api/campaigns/CONT1/") in calls
    assert "Sent campaign" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_delete_campaign_with_sibling_messages_removes_only_that_message(mock_build):
    ctx, calls = _campaign_ctx(["MSG1", "MSG2"])
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "campaign", "MSG1", "--confirm", "MSG1"])
    assert result.exit_code == 0, result.output
    assert ("DELETE", "/api/campaign-messages/MSG1/") in calls
    assert not any(p.startswith("/api/campaigns/CONT1/") and m == "DELETE" for m, p in calls)


def test_raw_delete_of_form_still_blocked():
    with pytest.raises(AuthError, match="klaviyo delete"):
        ensure_delete_allowed("/api/forms/F1/")


@patch("klaviyo_cli.cli.build_context")
def test_delete_form_uses_forms_revision_and_warns_when_live(mock_build):
    from klaviyo_cli.commands.forms import FORMS_REVISION
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, revision))
        if method == "DELETE":
            ensure_delete_allowed(path)
            return {"_raw": ""}
        return {"data": {"id": "F1", "attributes": {"name": "Popup", "status": "draft", "definition": {
            "versions": [{"id": 1, "status": "live"}, {"id": 2, "status": "draft"}]}}}}

    mock_build.return_value = {"call": call, "label": "t", "json": False}
    result = CliRunner().invoke(main, ["delete", "form", "F1", "--confirm", "F1"])
    assert result.exit_code == 0, result.output
    assert "Live form" in result.output
    assert ("DELETE", "/api/forms/F1/", FORMS_REVISION) in calls
    assert all(r == FORMS_REVISION for _, _, r in calls)

import copy
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from klaviyo_cli.cli import main
from klaviyo_cli.transport import AuthError, ensure_delete_allowed
from tests.test_campaigns import _api


def _email(aid, name, nxt=None, subject="Subj"):
    return {"id": aid, "type": "send-email", "links": {"next": nxt},
            "data": {"status": "draft", "message": {"name": name, "subject_line": subject, "template_id": "T" + aid,
                                                    "from_email": "hi@x.com", "smart_sending_enabled": True}}}


def _flow(status="draft", actions=None):
    actions = actions or [
        _email("e1", "EM1", "d1"),
        {"id": "d1", "type": "time-delay", "links": {"next": "e2"}, "data": {"unit": "days", "value": 2}},
        _email("e2", "EM2"),
    ]
    return {"data": {"attributes": {"name": "Welcome", "status": status, "definition": {
        "entry_action_id": "e1", "actions": actions}}}}


def _with(new_action, status="draft", connected=True):
    """The flow after an insert: the new step hangs off the last email, or floats unconnected."""
    flow = _flow(status)
    actions = flow["data"]["attributes"]["definition"]["actions"]
    if connected:
        actions[-1]["links"] = {"next": new_action["id"]}
    actions.append(new_action)
    return flow


def _run(routes, *args, stdin=None):
    ctx_obj, calls = _api(routes)
    with patch("klaviyo_cli.cli.build_context", return_value=ctx_obj):
        result = CliRunner().invoke(main, list(args), input=stdin)
    return result, calls


def _post(calls):
    return next(c[2]["data"]["attributes"] for c in calls if c[0] == "POST")


def test_add_delay_before_a_message_links_to_it():
    new = {"id": "n1", "type": "time-delay"}
    routes = [(("GET", "/api/flows/F1/"), [_flow(), _with(new)]),
              (("POST", "/api/flow-actions/"), {"data": {"id": "n1"}})]
    result, calls = _run(routes, "flow-add-delay", "F1", "--before", "2", "--hours", "6")
    assert result.exit_code == 0, result.output
    attrs = _post(calls)
    assert attrs["definition"]["links"] == {"next": "e2"}
    assert attrs["definition"]["data"] == {"unit": "hours", "value": 6, "timezone": "profile"}
    assert "anchor-action" not in attrs and "allow_live_action" not in attrs


def test_add_after_a_message_uses_an_anchor():
    routes = [(("GET", "/api/flows/F1/"), [_flow(), _with({"id": "n1", "type": "time-delay"})]),
              (("POST", "/api/flow-actions/"), {"data": {"id": "n1"}})]
    result, calls = _run(routes, "flow-add-delay", "F1", "--after", "2", "--days", "1")
    assert result.exit_code == 0, result.output
    attrs = _post(calls)
    assert "links" not in attrs["definition"]
    assert attrs["anchor-action"]["data"]["id"] == "e2"
    assert attrs["anchor-action"]["data"]["attributes"]["definition"]["links"] == {"next": "new"}


def test_live_flow_delay_needs_live_ok_and_then_opts_in():
    routes = [(("GET", "/api/flows/F1/"), [_flow("live"), _flow("live")])]
    result, calls = _run(routes, "flow-add-delay", "F1", "--before", "2", "--days", "1")
    assert result.exit_code != 0 and "--live-ok" in result.output
    assert not [c for c in calls if c[0] == "POST"]

    routes = [(("GET", "/api/flows/F1/"), [_flow("live"), _with({"id": "n1", "type": "time-delay"}, "live")]),
              (("POST", "/api/flow-actions/"), {"data": {"id": "n1"}})]
    result, calls = _run(routes, "flow-add-delay", "F1", "--before", "2", "--days", "1", "--live-ok")
    assert result.exit_code == 0, result.output
    assert _post(calls)["allow_live_action"] is True


def test_new_step_missing_or_unconnected_warns_against_retrying():
    for after in (_flow(), _with({"id": "n1", "type": "time-delay"}, connected=False)):
        routes = [(("GET", "/api/flows/F1/"), [_flow(), after]),
                  (("POST", "/api/flow-actions/"), {"data": {"id": "n1"}})]
        result, _ = _run(routes, "flow-add-delay", "F1", "--before", "2", "--days", "1")
        assert result.exit_code != 0 and "duplicate" in result.output


def test_manual_flows_are_gated_like_live_ones():
    result, calls = _run([(("GET", "/api/flows/F1/"), _flow("manual"))],
                         "flow-remove-step", "F1", "--delay-before", "2", "--yes")
    assert result.exit_code != 0 and "is manual" in result.output and "--live-ok" in result.output
    assert not [c for c in calls if c[0] == "DELETE"]


def test_set_delay_patches_the_delay_before_the_message():
    changed = copy.deepcopy(_flow())
    changed["data"]["attributes"]["definition"]["actions"][1]["data"] = {"unit": "days", "value": 3}
    routes = [(("GET", "/api/flows/F1/"), [_flow(), changed]), (("PATCH", "/api/flow-actions/d1/"), {})]
    result, calls = _run(routes, "flow-set-delay", "F1", "--before", "2", "--days", "3")
    assert result.exit_code == 0, result.output
    patched = next(c for c in calls if c[0] == "PATCH")[2]["data"]["attributes"]["definition"]
    assert patched == {"id": "d1", "type": "time-delay", "links": {"next": "e2"},
                       "data": {"unit": "days", "value": 3}}
    assert "2 days -> 3 days" in result.output


def test_set_delay_with_no_delay_there():
    result, _ = _run([(("GET", "/api/flows/F1/"), _flow())], "flow-set-delay", "F1", "--before", "1", "--days", "3")
    assert result.exit_code != 0 and "no delay directly before message 1" in result.output


def test_add_email_copies_another_and_overrides():
    routes = [(("GET", "/api/flows/F1/"), [_flow(), _with(_email("n1", "EM3"))]),
              (("POST", "/api/flow-actions/"), {"data": {"id": "n1"}})]
    result, calls = _run(routes, "flow-add-email", "F1", "--after", "2", "--copy", "1",
                         "--name", "EM3", "--subject", "Last chance")
    assert result.exit_code == 0, result.output
    d = _post(calls)["definition"]
    assert d["type"] == "send-email" and d["data"]["status"] == "draft"
    assert d["data"]["message"] == {"name": "EM3", "subject_line": "Last chance", "template_id": "Te1",
                                    "from_email": "hi@x.com", "smart_sending_enabled": True}


def test_add_email_needs_a_subject():
    result, _ = _run([(("GET", "/api/flows/F1/"), _flow())], "flow-add-email", "F1", "--after", "2")
    assert result.exit_code != 0 and "--subject" in result.output


def test_remove_asks_first_and_declining_removes_nothing():
    result, calls = _run([(("GET", "/api/flows/F1/"), _flow())], "flow-remove-step", "F1", "--message", "2",
                         stdin="n\n")
    assert result.exit_code != 0 and "Nothing removed" in result.output
    assert not [c for c in calls if c[0] == "DELETE"]


def test_remove_delay_deletes_through_the_confirmation_gate():
    gone = _flow(actions=[_email("e1", "EM1", "e2"), _email("e2", "EM2")])
    routes = [(("GET", "/api/flows/F1/"), [_flow(), gone]), (("DELETE", "/api/flow-actions/d1/"), {})]
    result, calls = _run(routes, "flow-remove-step", "F1", "--delay-before", "2", "--yes")
    assert result.exit_code == 0, result.output
    assert ("DELETE", "/api/flow-actions/d1/") in [c[:2] for c in calls]


def test_flow_action_deletes_are_blocked_outside_the_command():
    with pytest.raises(AuthError):
        ensure_delete_allowed("/api/flow-actions/d1/")


def _sms(aid, body, status="live"):
    return {"id": aid, "type": "send-sms", "links": {"next": None},
            "data": {"status": status, "message": {"name": "SMS", "body": body, "add_org_prefix": True,
                                                    "message_hierarchy": [
                                                        {"message": body, "message_format": "RCS", "extra": {}},
                                                        {"message": body, "message_format": "SMS", "extra": {}}]}}}


def _sms_flow(body, status="live"):
    email = _email("e1", "EM1", "s1")
    return _flow(status, [email, _sms("s1", body)])


def _patch(calls):
    return next(c[2]["data"]["attributes"]["definition"] for c in calls if c[0] == "PATCH")


def test_edit_sms_replace_updates_body_and_every_version_then_verifies():
    old, new = "expire on 2026-11-26. Go", "expire soon. Go"
    routes = [(("GET", "/api/flows/F1/"), [_sms_flow(old), _sms_flow(new)]),
              (("PATCH", "/api/flow-actions/s1/"), {"data": {}})]
    result, calls = _run(routes, "flow-edit-message", "F1", "--message", "2", "--replace", "on 2026-11-26", "soon")
    assert result.exit_code == 0, result.output
    msg = _patch(calls)["data"]["message"]
    assert msg["body"] == new
    assert [h["message"] for h in msg["message_hierarchy"]] == [new, new]
    assert msg["add_org_prefix"] is True  # other settings kept
    assert _patch(calls)["data"]["status"] == "live"
    assert "Saved and verified" in result.output


def test_edit_sms_body_from_file(tmp_path):
    f = tmp_path / "sms.txt"
    f.write_text("Brand new text\n")
    routes = [(("GET", "/api/flows/F1/"), [_sms_flow("Old"), _sms_flow("Brand new text")]),
              (("PATCH", "/api/flow-actions/s1/"), {"data": {}})]
    result, calls = _run(routes, "flow-edit-message", "F1", "--message", "2", "--body", f"@{f}")
    assert result.exit_code == 0, result.output
    assert _patch(calls)["data"]["message"]["body"] == "Brand new text"


def test_edit_dry_run_saves_nothing():
    routes = [(("GET", "/api/flows/F1/"), _sms_flow("Hello there"))]
    result, calls = _run(routes, "flow-edit-message", "F1", "--message", "2", "--replace", "there", "you", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Hello you" in result.output and "Dry run" in result.output
    assert not [c for c in calls if c[0] == "PATCH"]


def test_edit_replace_missing_text_fails_without_saving():
    routes = [(("GET", "/api/flows/F1/"), _sms_flow("Hello"))]
    result, calls = _run(routes, "flow-edit-message", "F1", "--message", "2", "--replace", "nope", "x")
    assert result.exit_code != 0 and "isn't in the SMS body" in result.output
    assert not [c for c in calls if c[0] == "PATCH"]


def test_edit_fails_loudly_if_klaviyo_does_not_keep_it():
    routes = [(("GET", "/api/flows/F1/"), [_sms_flow("Old"), _sms_flow("Old")]),
              (("PATCH", "/api/flow-actions/s1/"), {"data": {}})]
    result, _ = _run(routes, "flow-edit-message", "F1", "--message", "2", "--body", "New")
    assert result.exit_code != 0 and "didn't keep body" in result.output


def test_edit_email_subject_and_preview():
    after = _flow()
    after["data"]["attributes"]["definition"]["actions"][0]["data"]["message"].update(
        {"subject_line": "Last chance", "preview_text": "Ends tonight"})
    routes = [(("GET", "/api/flows/F1/"), [_flow(), after]),
              (("PATCH", "/api/flow-actions/e1/"), {"data": {}})]
    result, calls = _run(routes, "flow-edit-message", "F1", "--message", "1",
                         "--subject", "Last chance", "--preview", "Ends tonight")
    assert result.exit_code == 0, result.output
    msg = _patch(calls)["data"]["message"]
    assert msg["subject_line"] == "Last chance" and msg["preview_text"] == "Ends tonight"
    assert msg["template_id"] == "Te1"


def test_edit_wrong_option_for_message_type():
    result, _ = _run([(("GET", "/api/flows/F1/"), _flow())], "flow-edit-message", "F1", "--message", "1", "--body", "x")
    assert result.exit_code != 0 and "edit-template" in result.output
    result, _ = _run([(("GET", "/api/flows/F1/"), _sms_flow("x"))],
                     "flow-edit-message", "F1", "--message", "2", "--subject", "x")
    assert result.exit_code != 0 and "is SMS" in result.output


def _status_flow(flow_status, msg_status):
    f = _flow(flow_status)
    for a in f["data"]["attributes"]["definition"]["actions"]:
        if a["type"] == "send-email":
            a["data"]["status"] = msg_status
    return f


def test_set_status_live_all_messages_then_flow_in_order():
    routes = [(("GET", "/api/flows/F1/"), [_status_flow("draft", "draft"), _status_flow("live", "live")]),
              (("PATCH", "/api/flow-actions/e1/"), {"data": {}}),
              (("PATCH", "/api/flow-actions/e2/"), {"data": {}}),
              (("PATCH", "/api/flows/F1/"), {"data": {}})]
    result, calls = _run(routes, "flow-set-status", "F1", "live", "--all-messages", "--yes")
    assert result.exit_code == 0, result.output
    patches = [c[1] for c in calls if c[0] == "PATCH"]
    assert patches[-1].startswith("/api/flows/F1")  # flow goes live last
    defn = next(c[2] for c in calls if c[0] == "PATCH" and "flow-actions" in c[1])["data"]["attributes"]["definition"]
    assert defn["data"]["status"] == "live" and defn["data"]["message"]["subject_line"] == "Subj"
    assert "Saved and verified" in result.output


def test_set_status_draft_pauses_flow_first():
    routes = [(("GET", "/api/flows/F1/"), [_status_flow("live", "live"), _status_flow("draft", "draft")]),
              (("PATCH", "/api/flow-actions/e1/"), {"data": {}}),
              (("PATCH", "/api/flow-actions/e2/"), {"data": {}}),
              (("PATCH", "/api/flows/F1/"), {"data": {}})]
    result, calls = _run(routes, "flow-set-status", "F1", "draft", "--all-messages")
    assert result.exit_code == 0, result.output
    assert [c[1] for c in calls if c[0] == "PATCH"][0].startswith("/api/flows/F1")


def test_set_status_live_asks_first_and_dry_run_saves_nothing():
    routes = [(("GET", "/api/flows/F1/"), _status_flow("draft", "draft"))]
    result, calls = _run(routes, "flow-set-status", "F1", "live", "--all-messages", stdin="n\n")
    assert result.exit_code != 0 and "Nothing changed" in result.output
    assert not [c for c in calls if c[0] == "PATCH"]
    result, calls = _run(routes, "flow-set-status", "F1", "live", "--all-messages", "--dry-run")
    assert result.exit_code == 0 and "Dry run" in result.output
    assert not [c for c in calls if c[0] == "PATCH"]


def test_set_status_warns_when_flow_goes_live_with_draft_messages():
    routes = [(("GET", "/api/flows/F1/"), _status_flow("draft", "draft"))]
    result, _ = _run(routes, "flow-set-status", "F1", "live", "--dry-run")
    assert "won't send" in result.output


def test_set_status_flags_what_klaviyo_did_not_keep():
    routes = [(("GET", "/api/flows/F1/"), [_status_flow("draft", "draft"), _status_flow("draft", "draft")]),
              (("PATCH", "/api/flows/F1/"), {"data": {}})]
    result, _ = _run(routes, "flow-set-status", "F1", "live", "--yes")
    assert result.exit_code != 0 and "didn't keep" in result.output


def test_set_status_nothing_to_do():
    result, _ = _run([(("GET", "/api/flows/F1/"), _status_flow("live", "live"))],
                     "flow-set-status", "F1", "live", "--all-messages")
    assert result.exit_code != 0 and "already live" in result.output

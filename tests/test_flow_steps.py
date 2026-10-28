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

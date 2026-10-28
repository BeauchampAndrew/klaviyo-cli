from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main
from klaviyo_cli.commands.test_send import _message_steps
from tests.test_campaigns import _api, _message, _variation


def _email(aid, name, tpl, subject, nxt=None, **links):
    return {"id": aid, "type": "send-email", "links": links or {"next": nxt},
            "data": {"message": {"name": name, "template_id": tpl, "subject_line": subject,
                                 "preview_text": "pt", "from_email": "hi@example.com"}}}


FLOW = {"data": {"attributes": {"definition": {
    "entry_action_id": "split",
    "actions": [
        {"id": "split", "type": "conditional-split",
         "links": {"next_if_true": "e1", "next_if_false": "sms"}},
        _email("e1", "EM1", "T1", "Yes path"),
        {"id": "sms", "type": "send-sms", "links": {"next": "e3"}, "data": {"message": {"body": "x"}}},
        _email("e3", "EM3", "T3", "No path"),
    ]}}}}


def test_message_steps_number_like_flow_detail():
    """YES branch first, SMS steps counted, so 'EMAIL 3' here is flow-detail's EMAIL 3."""
    steps = _message_steps(FLOW["data"]["attributes"]["definition"])
    assert [(n, a["id"]) for n, a in steps] == [(1, "e1"), (2, "sms"), (3, "e3")]


@patch("klaviyo_cli.cli.build_context")
def test_campaign_test_send_uses_a_copy_named_after_the_subject(mock_build):
    """Klaviyo titles a test '<template name> Preview', so the send goes out from a
    throwaway copy named after the real subject, which is deleted afterwards."""
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message(variations=[_variation(subject="Big sale")])),
        (("POST", "/api/template-clone/"), {"data": {"id": "COPY"}}),
        (("POST", "/api/template-preview-send-jobs/"), {"data": {"id": "J1"}}),
        (("DELETE", "/api/templates/COPY/"), {}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["test-send", "MSG1", "--to", "a@x.com, b@x.com"])
    assert result.exit_code == 0, result.output
    clone = next(c for c in calls if c[1] == "/api/template-clone/")
    assert clone[2]["data"]["id"] == "TPL1"
    assert clone[2]["data"]["attributes"]["name"] == "Big sale"
    send = next(c for c in calls if c[1] == "/api/template-preview-send-jobs/")
    assert send[2]["data"]["attributes"]["recipients"] == ["a@x.com", "b@x.com"]
    assert send[2]["data"]["relationships"]["template"]["data"]["id"] == "COPY"
    assert calls[-1][:2] == ("DELETE", "/api/templates/COPY/")
    assert '"Big sale Preview"' in result.output


@patch("klaviyo_cli.cli.build_context")
def test_copy_is_deleted_even_when_the_send_fails(mock_build):
    from klaviyo_cli.transport import APIError
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("POST", "/api/template-clone/"), {"data": {"id": "COPY"}}),
        (("POST", "/api/template-preview-send-jobs/"), APIError("daily limit reached")),
        (("DELETE", "/api/templates/COPY/"), {}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["test-send", "MSG1", "--to", "a@x.com"])
    assert result.exit_code != 0
    assert "daily limit" in result.output
    assert ("DELETE", "/api/templates/COPY/") in [c[:2] for c in calls]


@patch("klaviyo_cli.cli.build_context")
def test_more_than_five_recipients_are_split_into_batches(mock_build):
    ctx_obj, calls = _api([(("POST", "/api/template-preview-send-jobs/"), {"data": {"id": "J"}})])
    mock_build.return_value = ctx_obj
    to = ",".join(f"p{i}@x.com" for i in range(7))
    result = CliRunner().invoke(main, ["test-send", "--template", "TPL9", "--to", to])
    assert result.exit_code == 0, result.output
    sizes = [len(c[2]["data"]["attributes"]["recipients"]) for c in calls]
    assert sizes == [5, 2]
    assert not any(c[1] == "/api/template-clone/" for c in calls)


@patch("klaviyo_cli.cli.build_context")
def test_flow_emails_by_number(mock_build):
    ctx_obj, calls = _api([
        (("GET", "/api/flows/FL1/"), FLOW),
        (("POST", "/api/template-clone/"), {"data": {"id": "COPY"}}),
        (("POST", "/api/template-preview-send-jobs/"), {"data": {"id": "J"}}),
        (("DELETE", "/api/templates/COPY/"), {}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["test-send", "--flow", "FL1", "--email", "3", "--to", "a@x.com"])
    assert result.exit_code == 0, result.output
    clone = next(c for c in calls if c[1] == "/api/template-clone/")
    assert clone[2]["data"]["id"] == "T3"
    assert "EMAIL 3: EM3" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_flow_email_number_that_is_an_sms_step_is_rejected(mock_build):
    ctx_obj, calls = _api([(("GET", "/api/flows/FL1/"), FLOW)])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["test-send", "--flow", "FL1", "--email", "2", "--to", "a@x.com"])
    assert result.exit_code != 0
    assert "emails are 1, 3" in result.output
    assert all(c[0] == "GET" for c in calls)


@patch("klaviyo_cli.cli.build_context")
def test_as_profile_fills_merge_tags_from_that_profile(mock_build):
    ctx_obj, calls = _api([
        (("GET", "/api/profiles/"), {"data": [{"id": "PROF1"}]}),
        (("POST", "/api/template-preview-send-jobs/"), {"data": {"id": "J"}}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["test-send", "--template", "TPL9", "--to", "a@x.com",
                                       "--as-profile", "jane@x.com"])
    assert result.exit_code == 0, result.output
    send = calls[-1][2]
    assert send["data"]["relationships"]["profile"]["data"]["id"] == "PROF1"


@patch("klaviyo_cli.cli.build_context")
def test_needs_exactly_one_target(mock_build):
    ctx_obj, calls = _api([])
    mock_build.return_value = ctx_obj
    for args in (["--to", "a@x.com"], ["MSG1", "--template", "T", "--to", "a@x.com"],
                 ["--flow", "FL1", "--to", "a@x.com"]):
        result = CliRunner().invoke(main, ["test-send", *args])
        assert result.exit_code != 0, args
    assert calls == []

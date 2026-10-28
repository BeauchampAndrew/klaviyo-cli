from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main
from klaviyo_cli.transport import APIError
from tests.test_campaigns import _api, _message


def _row(rid, used, cap, unit="profiles", as_of="2026-10-28T00:00:00+00:00"):
    return {"id": rid, "attributes": {"usage_used": used, "usage_max": cap, "unit": unit,
                                      "period_start": "2026-10-02T00:00:00+00:00",
                                      "period_end": "2026-11-01T00:00:00+00:00", "as_of": as_of}}


def _run(routes, *args):
    ctx_obj, calls = _api(routes)
    with patch("klaviyo_cli.cli.build_context", return_value=ctx_obj):
        result = CliRunner().invoke(main, list(args))
    return result, calls


def test_billing_flags_core_caps_and_projection_but_not_add_ons():
    rows = [_row("customer_hub_active_profiles", 149850, 250),
            _row("active_profiles", 149850, 140000),
            _row("email_sends", 400000, 1400000, "messages"),
            _row("sms_credits", 600, 790, "USD")]  # 26 of 30 days gone: pace ~88%
    result, _ = _run([(("GET", "/api/billing-usage/"), {"data": rows})], "billing-usage")
    assert result.exit_code == 3, result.output
    lines = {l.split()[0] + l.split()[1]: l for l in result.output.splitlines() if l.strip()}
    assert "<--" in lines["Activeprofiles"]
    assert "<--" in lines["SMScredits"] and "~88%" in lines["SMScredits"]
    assert "<--" not in lines["Emailsends"]
    assert "<--" not in lines["CustomerHub"]
    assert result.output.index("Active profiles") < result.output.index("Add-on products")


def test_billing_under_threshold_exits_cleanly():
    result, _ = _run([(("GET", "/api/billing-usage/"), {"data": [_row("active_profiles", 1000, 140000)]})],
                     "billing-usage")
    assert result.exit_code == 0, result.output


def test_billing_missing_scope_explains_the_key_fix():
    routes = [(("GET", "/api/billing-usage/"), APIError("Your API key is missing required scopes: usage:read"))]
    result, _ = _run(routes, "billing-usage")
    assert result.exit_code == 1 and "Usage: Read Access" in result.output


def _job(status, new=None, error=None):
    rels = {"converted-template": {"data": {"type": "template", "id": new}}} if new else {}
    return {"data": {"id": "J1", "attributes": {"status": status, "error": error}, "relationships": rels}}


def _code_tpl(editor="CODE"):
    return {"data": {"attributes": {"name": "Promo", "editor_type": editor}}}


@patch("klaviyo_cli.commands.convert.time.sleep")
def test_convert_polls_until_complete(_sleep):
    routes = [(("GET", "/api/templates/T1/"), _code_tpl()),
              (("POST", "/api/template-conversion-jobs/"), _job("queued")),
              (("GET", "/api/template-conversion-jobs/J1/"), [_job("processing"), _job("complete", "NEW")]),
              (("PATCH", "/api/templates/NEW/"), {})]
    result, calls = _run(routes, "convert-template", "T1", "--name", "DnD")
    assert result.exit_code == 0, result.output
    assert "Created drag-and-drop template NEW" in result.output
    assert next(c for c in calls if c[0] == "PATCH")[2]["data"]["attributes"] == {"name": "DnD"}


@patch("klaviyo_cli.commands.convert.time.sleep")
def test_convert_failure_reports_klaviyos_reason(_sleep):
    routes = [(("GET", "/api/templates/T1/"), _code_tpl()),
              (("POST", "/api/template-conversion-jobs/"), _job("queued")),
              (("GET", "/api/template-conversion-jobs/J1/"), _job("failed", error="bad markup"))]
    result, _ = _run(routes, "convert-template", "T1")
    assert result.exit_code != 0 and "bad markup" in result.output


def test_convert_refuses_non_html_templates():
    result, calls = _run([(("GET", "/api/templates/T1/"), _code_tpl("SYSTEM_DRAGGABLE"))], "convert-template", "T1")
    assert result.exit_code != 0 and "nothing to convert" in result.output
    assert not [c for c in calls if c[0] == "POST"]


@patch("klaviyo_cli.commands.convert.time.sleep")
def test_convert_campaign_assigns_then_deletes_the_standalone_copy(_sleep):
    variation = {"data": {"id": "VAR1", "attributes": {"definition": {"details": {"template_id": "LIVE"}}}}}
    routes = [(("GET", "/api/campaign-messages/MSG1/"), _message()),
              (("GET", "/api/templates/TPL1/"), _code_tpl()),
              (("GET", "/api/templates/LIVE/"), _code_tpl("SYSTEM_DRAGGABLE")),
              (("POST", "/api/template-conversion-jobs/"), _job("queued")),
              (("GET", "/api/template-conversion-jobs/J1/"), _job("complete", "NEW")),
              (("POST", "/api/campaign-message-assign-template/"), {}),
              (("GET", "/api/campaign-variations/VAR1/"), variation),
              (("DELETE", "/api/templates/NEW/"), {})]
    result, calls = _run(routes, "convert-template", "--campaign", "MSG1")
    assert result.exit_code == 0, result.output
    assign = next(c for c in calls if c[1] == "/api/campaign-message-assign-template/")[2]["data"]
    assert assign["id"] == "VAR1" and assign["relationships"]["template"]["data"]["id"] == "NEW"
    assert ("DELETE", "/api/templates/NEW/") in [c[:2] for c in calls]
    assert "now uses the drag-and-drop version (template LIVE)" in result.output


def test_convert_campaign_must_be_a_draft():
    result, calls = _run([(("GET", "/api/campaign-messages/MSG1/"), _message(status="Scheduled"))],
                         "convert-template", "--campaign", "MSG1")
    assert result.exit_code != 0 and "only a draft" in result.output
    assert not [c for c in calls if c[0] == "POST"]

import copy
import json
from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main
from klaviyo_cli.commands.forms import FORMS_REVISION


def _fake_ctx_factory(responses):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, copy.deepcopy(body), revision))
        return responses.pop(0)

    return {"call": call, "label": "test-account"}, calls


def _version(vid, status, placeholder):
    return {
        "id": vid, "status": status, "type": "popup", "name": "Desktop", "ab_test": False,
        "triggers": [{"id": "T1", "type": "delay", "properties": {"seconds": 5}}],
        "steps": [{"id": "S1", "columns": [{"id": "C1", "rows": [{"id": "R1", "blocks": [
            {"id": "B1", "type": "email", "properties": {"label": "Email", "placeholder": placeholder}},
            {"id": "B2", "type": "button", "properties": {"label": "Subscribe"},
             "action": {"id": "A1", "type": "next_step", "properties": {}}},
        ]}]}]}],
    }


def _form(live_text="Enter you email", draft_text="Enter you email", with_draft=True):
    versions = [_version(100, "live", live_text)]
    if with_draft:
        versions.append(_version(200, "draft", draft_text))
    return {"data": {"type": "form", "id": "AbC123", "attributes": {
        "name": "Popup", "status": "draft", "definition": {"versions": versions}}}}


@patch("klaviyo_cli.cli.build_context")
def test_form_shows_blocks_and_live_status(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_form()])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["form", "AbC123"])

    assert result.exit_code == 0, result.output
    assert "(live, with draft)" in result.output
    assert "Email | Enter you email" in result.output
    assert "-> next_step" in result.output
    assert calls[0][3] == FORMS_REVISION


@patch("klaviyo_cli.cli.build_context")
def test_form_handles_non_text_block_content(mock_build):
    form = _form()
    blocks = form["data"]["attributes"]["definition"]["versions"][0]["steps"][0]["columns"][0]["rows"][0]["blocks"]
    blocks.append({"id": "B3", "type": "sms_disclosure", "properties": {"content": {"type": "custom"}}})
    ctx_obj, _ = _fake_ctx_factory([form])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["form", "AbC123"])

    assert result.exit_code == 0, result.output
    assert "sms_disclosure" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_form_warns_when_no_draft(mock_build):
    ctx_obj, _ = _fake_ctx_factory([_form(with_draft=False)])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["form", "AbC123"])

    assert result.exit_code == 0, result.output
    assert "No draft version" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_form_replace_edits_draft_only_and_verifies(mock_build):
    after = _form(draft_text="Enter your email")
    ctx_obj, calls = _fake_ctx_factory([_form(), {"data": {}}, after])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["form-replace", "AbC123", "Enter you email", "Enter your email"])

    assert result.exit_code == 0, result.output
    method, path, body, revision = calls[1]
    assert (method, path, revision) == ("PATCH", "/api/forms/AbC123/", FORMS_REVISION)
    live, draft = body["data"]["attributes"]["definition"]["versions"]
    assert live == _version(100, "live", "Enter you email")  # echoed back untouched
    assert draft["steps"][0]["columns"][0]["rows"][0]["blocks"][0]["properties"]["placeholder"] == "Enter your email"
    assert draft["id"] == 200


@patch("klaviyo_cli.cli.build_context")
def test_form_replace_dry_run_saves_nothing(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_form()])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["form-replace", "AbC123", "Subscribe", "Join", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry run: 1 change(s)" in result.output
    assert [c[0] for c in calls] == ["GET"]


@patch("klaviyo_cli.cli.build_context")
def test_form_replace_text_only_in_live_version(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_form(live_text="Old live copy", draft_text="Other")])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["form-replace", "AbC123", "Old live copy", "New"])

    assert result.exit_code != 0
    assert "only in the live version" in result.output
    assert len(calls) == 1


@patch("klaviyo_cli.cli.build_context")
def test_update_form_restores_live_version(mock_build, tmp_path):
    edited = _form()["data"]["attributes"]["definition"]
    edited["versions"][0]["name"] = "edited live"
    edited["versions"][1]["name"] = "edited draft"
    f = tmp_path / "def.json"
    f.write_text(json.dumps(edited))
    ctx_obj, calls = _fake_ctx_factory([_form(), {"data": {}}])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["update-form", "AbC123", "--definition", f"@{f}"])

    assert result.exit_code == 0, result.output
    assert "edits to it were dropped" in result.output
    live, draft = calls[1][2]["data"]["attributes"]["definition"]["versions"]
    assert live["name"] == "Desktop"
    assert draft["name"] == "edited draft"


@patch("klaviyo_cli.cli.build_context")
def test_update_form_rejects_version_mismatch(mock_build, tmp_path):
    f = tmp_path / "def.json"
    f.write_text(json.dumps({"versions": [{"id": 999, "status": "draft"}]}))
    ctx_obj, calls = _fake_ctx_factory([_form()])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["update-form", "AbC123", "--definition", f"@{f}"])

    assert result.exit_code != 0
    assert "can't be added or removed" in result.output
    assert len(calls) == 1


@patch("klaviyo_cli.cli.build_context")
def test_copy_form_strips_ids_and_creates_draft(mock_build):
    created = {"data": {"id": "NeW999", "attributes": {"definition": {"versions": [{"id": 300}]}}}}
    ctx_obj, calls = _fake_ctx_factory([_form(), created])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["copy-form", "AbC123", "--name", "Desktop copy"])

    assert result.exit_code == 0, result.output
    assert "Created draft form [NeW999]" in result.output
    method, path, body, revision = calls[1]
    assert (method, path, revision) == ("POST", "/api/forms/", FORMS_REVISION)
    attrs = body["data"]["attributes"]
    assert attrs["status"] == "draft" and attrs["ab_test"] is False
    (version,) = attrs["definition"]["versions"]
    assert version["status"] == "draft"
    assert '"id"' not in json.dumps(version)


@patch("klaviyo_cli.cli.build_context")
def test_publish_form_requires_confirm(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_form()])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["publish-form", "AbC123"])

    assert result.exit_code != 0
    assert "--confirm AbC123" in result.output
    assert [c[0] for c in calls] == ["GET"]


@patch("klaviyo_cli.cli.build_context")
def test_publish_form_posts_job(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_form(), {}, _form(with_draft=False)])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["publish-form", "AbC123", "--confirm", "AbC123"])

    assert result.exit_code == 0, result.output
    method, path, body, _ = calls[1]
    assert (method, path) == ("POST", "/api/form-publish-jobs/")
    assert body["data"]["relationships"]["form"]["data"]["id"] == "AbC123"
    assert "now live" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_copy_form_drops_what_create_rejects(mock_build):
    form = _form(with_draft=False)
    v = form["data"]["attributes"]["definition"]["versions"][0]
    v["steps"][0]["columns"][0]["rows"].append({"id": "R2", "blocks": []})
    v["triggers"].append({"id": "T2", "type": "url_patterns", "properties": {"allow_list": None, "deny_list": None}})
    v["teasers"] = [{"id": "X1", "device_type": "mobile", "content": "A"},
                    {"id": "X2", "device_type": "mobile", "content": "B"}]
    created = {"data": {"id": "NeW999", "attributes": {"definition": {"versions": [{"id": 300}]}}}}
    ctx_obj, calls = _fake_ctx_factory([form, created])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["copy-form", "AbC123", "--name", "Copy"])

    assert result.exit_code == 0, result.output
    assert "Dropped a second mobile teaser" in result.output
    (version,) = calls[1][2]["data"]["attributes"]["definition"]["versions"]
    assert len(version["steps"][0]["columns"][0]["rows"]) == 1
    assert [t["type"] for t in version["triggers"]] == ["delay"]
    assert [t["content"] for t in version["teasers"]] == ["A"]


@patch("klaviyo_cli.cli.build_context")
def test_form_replace_strips_invisible_junk_from_drafts_only(mock_build):
    form = _form()
    for v in form["data"]["attributes"]["definition"]["versions"]:
        v["steps"][0]["columns"][0]["rows"].append({"id": "E", "blocks": []})
        v["triggers"].append({"id": "U", "type": "url_patterns", "properties": {"allow_list": None, "deny_list": None}})
    ctx_obj, calls = _fake_ctx_factory([form, {"data": {}}, _form(draft_text="Enter your email")])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["form-replace", "AbC123", "Enter you email", "Enter your email"])

    assert result.exit_code == 0, result.output
    live, draft = calls[1][2]["data"]["attributes"]["definition"]["versions"]
    assert all(r["blocks"] for r in draft["steps"][0]["columns"][0]["rows"])
    assert "url_patterns" not in [t["type"] for t in draft["triggers"]]
    # live is read-only: sent back exactly as received
    assert live == form["data"]["attributes"]["definition"]["versions"][0]


@patch("klaviyo_cli.cli.build_context")
def test_copy_form_turns_ab_variation_into_plain_form(mock_build):
    form = _form(with_draft=False)
    form["data"]["attributes"]["definition"]["versions"][0]["ab_test"] = True
    created = {"data": {"id": "NeW1", "attributes": {"definition": {"versions": [{"id": 9}]}}}}
    ctx_obj, calls = _fake_ctx_factory([form, created])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["copy-form", "AbC123", "--name", "Winner"])

    assert result.exit_code == 0, result.output
    attrs = calls[1][2]["data"]["attributes"]
    assert attrs["ab_test"] is False
    assert attrs["definition"]["versions"][0]["ab_test"] is False


@patch("klaviyo_cli.cli.build_context")
def test_ab_test_form_creates_one_flagged_version_per_variant(mock_build):
    created = {"data": {"id": "AbT1", "attributes": {"definition": {"versions": [
        {"id": 11, "name": "Control", "ab_test": True},
        {"id": 12, "name": "10% off", "ab_test": True},
        {"id": 13, "name": "Test", "ab_test": False},
    ]}}}}
    ctx_obj, calls = _fake_ctx_factory([_form(), created])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["ab-test-form", "AbC123", "--name", "Test",
                                       "--variant", "Control", "--variant", "10% off"])

    assert result.exit_code == 0, result.output
    method, path, body, revision = calls[1]
    assert (method, path, revision) == ("POST", "/api/forms/", FORMS_REVISION)
    attrs = body["data"]["attributes"]
    assert attrs["ab_test"] is True and attrs["status"] == "draft"
    versions = attrs["definition"]["versions"]
    assert [v["name"] for v in versions] == ["Control", "10% off"]
    assert all(v["ab_test"] and v["status"] == "draft" and "id" not in v for v in versions)
    # copied from the live version, not the draft
    assert all(v["steps"][0]["columns"][0]["rows"][0]["blocks"][0]["properties"]["placeholder"]
               == "Enter you email" for v in versions)
    assert "Variation 10% off: version 12" in result.output
    assert "Base version (added by Klaviyo, not in the test): 13" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_ab_test_form_needs_two_distinct_variants(mock_build):
    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj
    one = CliRunner().invoke(main, ["ab-test-form", "AbC123", "--name", "T", "--variant", "A"])
    dup = CliRunner().invoke(main, ["ab-test-form", "AbC123", "--name", "T", "--variant", "A", "--variant", "A"])
    assert one.exit_code != 0 and dup.exit_code != 0
    assert calls == []

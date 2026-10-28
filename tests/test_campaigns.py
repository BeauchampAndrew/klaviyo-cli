from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli._util import _parse_send_time
from klaviyo_cli.cli import main
from klaviyo_cli.commands.campaigns import LEGACY_REVISION
from klaviyo_cli.transport import APIError


def test_parse_send_time_est():
    assert _parse_send_time("04-11-2026", "3:00 PM EST") == "2026-04-11T15:00:00-04:00"


def test_parse_send_time_24h_cst():
    assert _parse_send_time("2026-04-11", "15:00 CST") == "2026-04-11T15:00:00-05:00"


def _api(routes):
    """Fake ctx answering by (METHOD, path prefix), first match wins. A route
    value that is an Exception is raised (e.g. a 404 APIError)."""
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body, revision))
        for (m, prefix), resp in routes:
            if m == method and path.startswith(prefix):
                if isinstance(resp, list):  # successive responses, last one repeats
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected call {method} {path}")

    return {"call": call, "label": "test-account"}, calls


def _variation(vid="VAR1", channel="email", **details):
    base = {"channel": channel, "template_id": "TPL1", "from_label": "Example",
            "from_email": "support@example.com", "reply_to_email": "",
            "subject": "Spring sale starts today", "preview_text": ""}
    base.update(details)
    return {"type": "campaign-variation", "id": vid,
            "attributes": {"definition": {"name": "v", "details": base}}}


def _message(mid="MSG1", status="draft", send_time=None, variations=None,
             name="[09-20-2026] Trial Offer", container="CONT1", audience="AUD1"):
    variations = variations if variations is not None else [_variation()]
    return {
        "data": {
            "type": "campaign-message", "id": mid,
            "attributes": {"created": "2026-09-01T10:00:00+00:00", "definition": {
                "name": name, "status": status,
                "send_options": {"send_time": send_time,
                                 "send_strategy": "static" if send_time else None}}},
            "relationships": {
                "campaign": {"data": {"type": "campaign", "id": container}},
                "campaign-audience": {"data": {"type": "campaign-audience", "id": audience}},
                "campaign-variations": {"data": [{"type": "campaign-variation", "id": v["id"]}
                                                 for v in variations]},
            },
        },
        "included": [{"type": "campaign", "id": container}] + variations,
    }


def _audience(included=("SEGA",), excluded=()):
    return {"data": {"type": "campaign-audience", "id": "AUD1", "attributes": {
        "definition": {"included": list(included), "excluded": list(excluded)}}}}


def _list_page(messages, next_link=None):
    """A campaign-messages list page: message rows + included variations."""
    data, included = [], []
    for m in messages:
        data.append(m["data"])
        included += [i for i in m["included"] if i["type"] == "campaign-variation"]
    return {"data": data, "included": included, "links": {"next": next_link}}


def _not_found():
    return APIError("Campaign message not found.")


# ---------------------------------------------------------------------------
# lists
# ---------------------------------------------------------------------------


@patch("klaviyo_cli.cli.build_context")
def test_list_campaigns_paginates_and_keeps_old_json_shape(mock_build):
    """Rows come from /api/campaign-messages, but --json keeps the shape scripts
    already read (id + attributes.name/status/send_time), status capitalized."""
    page1 = _list_page([_message(status="sent", send_time="2026-07-01T10:00:00+00:00",
                                 name="July Promo")],
                       next_link="https://a.klaviyo.com/api/campaign-messages/?page%5Bcursor%5D=N")
    ctx_obj, calls = _api([(("GET", "/api/campaign-messages/?page"), _list_page([])),
                           (("GET", "/api/campaign-messages/?"), page1)])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["--json", "list-campaigns"])
    assert result.exit_code == 0, result.output
    assert '"name": "July Promo"' in result.output
    assert '"status": "Sent"' in result.output
    assert '"send_time": "2026-07-01T10:00:00+00:00"' in result.output
    assert len(calls) == 2


@patch("klaviyo_cli.cli.build_context")
def test_list_campaigns_maps_old_status_and_filters_channel_locally(mock_build):
    """--status Sent becomes equals(status,'sent'). Channel lives on the
    variation, so an SMS row is dropped client-side for --channel email."""
    page = _list_page([
        _message(mid="E1", status="sent", name="Email one"),
        _message(mid="S1", status="sent", name="SMS one",
                 variations=[_variation(vid="V2", channel="sms", body="hi")]),
    ])
    ctx_obj, calls = _api([(("GET", "/api/campaign-messages/?"), page)])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["list-campaigns", "--status", "Sent", "--channel", "email"])
    assert result.exit_code == 0, result.output
    assert "Email one" in result.output
    assert "SMS one" not in result.output
    assert "equals%28status%2C%27sent%27%29" in calls[0][1]


@patch("klaviyo_cli.cli.build_context")
def test_list_campaigns_rejects_unknown_status(mock_build):
    ctx_obj, calls = _api([])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["list-campaigns", "--status", "Paused"])
    assert result.exit_code != 0
    assert "draft, scheduled" in result.output
    assert calls == []


@patch("klaviyo_cli.cli.build_context")
def test_search_with_empty_query_lists_everything(mock_build):
    """The billing check runs search-campaigns "" and filters send_time itself.
    contains(name,'') would be a pointless filter, so send none."""
    ctx_obj, calls = _api([(("GET", "/api/campaign-messages/?"), _list_page([_message()]))])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["search-campaigns", ""])
    assert result.exit_code == 0, result.output
    assert "filter=" not in calls[0][1]


# ---------------------------------------------------------------------------
# get-campaign + ID resolution
# ---------------------------------------------------------------------------


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_reads_message_variation_and_audience(mock_build):
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message(
            status="scheduled", send_time="2026-09-20T14:00:00+00:00",
            variations=[_variation(subject="Seen on you", preview_text="Now in Black.")])),
        (("GET", "/api/campaign-audiences/AUD1/"), _audience(("SEGA",), ("SEGX",))),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "MSG1"])
    assert result.exit_code == 0, result.output
    out = result.output
    for expected in ("Status: Scheduled", "Subject: Seen on you", "Preview: Now in Black.",
                     "Send Time: 2026-09-20T14:00:00+00:00", "Include: SEGA", "Exclude: SEGX"):
        assert expected in out, expected
    # Scheduled: no need for the legacy planned-date lookup.
    assert all(c[3] != LEGACY_REVISION for c in calls)


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_accepts_container_id(mock_build):
    """A new-style container ID falls back to the container's only message."""
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/CONT1/"), _not_found()),
        (("GET", "/api/campaigns/CONT1/campaign-messages/"), {"data": [{"id": "MSG1"}]}),
        (("GET", "/api/campaign-messages/MSG1/"), _message(status="sent",
                                                           send_time="2026-09-20T14:00:00+00:00")),
        (("GET", "/api/campaign-audiences/AUD1/"), _audience()),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "CONT1"])
    assert result.exit_code == 0, result.output
    assert "ID: MSG1" in result.output
    assert "container campaign CONT1" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_unknown_id_says_not_found(mock_build):
    """An unknown container ID returns an empty message list, not a 404."""
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/NOPE/"), _not_found()),
        (("GET", "/api/campaigns/NOPE/campaign-messages/"), {"data": []}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "NOPE"])
    assert result.exit_code != 0
    assert "No campaign or campaign message with ID NOPE" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_container_with_several_messages_lists_their_ids(mock_build):
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/CONT1/"), _not_found()),
        (("GET", "/api/campaigns/CONT1/campaign-messages/"), {"data": [{"id": "M1"}, {"id": "M2"}]}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-message", "CONT1", "--subject", "X"])
    assert result.exit_code != 0
    assert "M1, M2" in result.output
    assert "--message-id" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_shows_planned_send_time_on_unscheduled_draft(mock_build):
    """A draft's planned date (set by patch-campaign) has no field at 2026-10-15,
    so it's read at LEGACY_REVISION. Printing 'not scheduled' hid a real date."""
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("GET", "/api/campaign-audiences/AUD1/"), _audience()),
        (("GET", "/api/campaigns/MSG1/"), {"data": {"attributes": {"send_strategy": {
            "method": "static", "datetime": "2026-09-20T14:00:00+00:00"}}}}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "MSG1"])
    assert result.exit_code == 0, result.output
    assert "2026-09-20T14:00:00+00:00 (planned, not scheduled yet)" in result.output
    assert calls[-1][3] == LEGACY_REVISION


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_without_any_date_still_says_not_scheduled(mock_build):
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("GET", "/api/campaign-audiences/AUD1/"), _audience()),
        (("GET", "/api/campaigns/MSG1/"), {"data": {"attributes": {}}}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "MSG1"])
    assert result.exit_code == 0, result.output
    assert "Send Time: not scheduled" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_campaign_names_flag_resolves_segment_and_list_names(mock_build):
    """--names prints each audience ID with its name. IDs can be segments or lists;
    a 404 on the segment lookup falls through to the list lookup."""
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message(status="sent",
                                                           send_time="2026-09-20T14:00:00+00:00")),
        (("GET", "/api/campaign-audiences/AUD1/"), _audience(("SEGA", "LIST1"), ("GONE1",))),
        (("GET", "/api/segments/SEGA/"), {"data": {"attributes": {"name": "Engaged 0-30d"}}}),
        (("GET", "/api/segments/LIST1/"), APIError("HTTP 404")),
        (("GET", "/api/lists/LIST1/"), {"data": {"attributes": {"name": "SEED LIST"}}}),
        (("GET", "/api/segments/GONE1/"), APIError("HTTP 404")),
        (("GET", "/api/lists/GONE1/"), APIError("HTTP 404")),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-campaign", "MSG1", "--names"])
    assert result.exit_code == 0, result.output
    assert "SEGA (Engaged 0-30d)" in result.output
    assert "LIST1 (SEED LIST)" in result.output
    assert "Exclude: GONE1" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_creative_reads_template_from_variation_and_greps(mock_build):
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message(variations=[_variation(subject="15% off")])),
        (("GET", "/api/templates/TPL1/"), {"data": {"attributes": {
            "text": "Intro line\nUse code FATHER26 at checkout\nFooter"}}}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-creative", "MSG1", "--grep", "code"])
    assert result.exit_code == 0, result.output
    assert "Subject: 15% off" in result.output
    assert "FATHER26" in result.output
    assert "Footer" not in result.output


# ---------------------------------------------------------------------------
# patch-campaign / schedule
# ---------------------------------------------------------------------------


@patch("klaviyo_cli.cli.build_context")
def test_patch_campaign_audiences_go_to_the_campaign_audience(mock_build):
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("PATCH", "/api/campaign-audiences/AUD1/"), _audience(("SEGA",), ("SEGX",))),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-campaign", "MSG1", "--include", "SEGA",
                                       "--exclude", "SEGX"])
    assert result.exit_code == 0, result.output
    method, path, body, _ = calls[-1]
    assert (method, path) == ("PATCH", "/api/campaign-audiences/AUD1/")
    assert body["data"]["attributes"]["definition"] == {"included": ["SEGA"], "excluded": ["SEGX"]}


@patch("klaviyo_cli.cli.build_context")
def test_patch_campaign_date_uses_legacy_revision_and_reports_planned_time(mock_build):
    """At 2026-10-15 a send_time PATCH on a draft is silently dropped, so the
    planned date is set where it still exists. It used to print 'not set'
    right after setting a date; it must show the planned time."""
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("PATCH", "/api/campaigns/MSG1/"), {"data": {"id": "MSG1", "attributes": {
            "send_time": None,
            "send_strategy": {"method": "static", "datetime": "2026-09-20T14:00:00+00:00"}}}}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-campaign", "MSG1", "--date", "09-20-2026",
                                       "--time", "10:00 AM EDT"])
    assert result.exit_code == 0, result.output
    method, path, body, revision = calls[-1]
    assert revision == LEGACY_REVISION
    assert body["data"]["attributes"]["send_strategy"]["datetime"] == "2026-09-20T10:00:00-04:00"
    assert "not set" not in result.output
    assert "2026-09-20T14:00:00+00:00" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_schedule_resolves_container_and_sends_message_id_at_legacy_revision(mock_build):
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/CONT1/"), _not_found()),
        (("GET", "/api/campaigns/CONT1/campaign-messages/"), {"data": [{"id": "MSG1"}]}),
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("POST", "/api/campaign-send-jobs/"), {"data": {"id": "MSG1"}}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["schedule", "CONT1"])
    assert result.exit_code == 0, result.output
    method, path, body, revision = calls[-1]
    assert body["data"]["id"] == "MSG1"
    assert revision == LEGACY_REVISION
    assert "MSG1 scheduled" in result.output


# ---------------------------------------------------------------------------
# patch-message
# ---------------------------------------------------------------------------


@patch("klaviyo_cli.cli.build_context")
def test_patch_message_patches_only_changed_fields_and_verifies(mock_build):
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("PATCH", "/api/campaign-variations/VAR1/"), {}),
        (("GET", "/api/campaign-variations/VAR1/"),
         {"data": _variation(subject="Last day of the spring sale")}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-message", "MSG1", "--subject", "Last day of the spring sale"])
    assert result.exit_code == 0, result.output
    method, path, body, _ = calls[1]
    assert (method, path) == ("PATCH", "/api/campaign-variations/VAR1/")
    assert body["data"]["attributes"]["definition"]["details"] == {
        "channel": "email", "subject": "Last day of the spring sale"}
    assert "Spring sale starts today" in result.output
    assert "from_email unchanged: support@example.com" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_patch_message_fails_loudly_when_change_did_not_stick(mock_build):
    """A 2xx is not proof. Re-read the variation and fail if the subject didn't change."""
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("PATCH", "/api/campaign-variations/VAR1/"), {}),
        (("GET", "/api/campaign-variations/VAR1/"), {"data": _variation()}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-message", "MSG1", "--subject", "Last day"])
    assert result.exit_code != 0
    assert "still shows the old subject" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_patch_message_fails_when_another_field_changed(mock_build):
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message()),
        (("PATCH", "/api/campaign-variations/VAR1/"), {}),
        (("GET", "/api/campaign-variations/VAR1/"),
         {"data": _variation(subject="Last day", from_email="")}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-message", "MSG1", "--subject", "Last day"])
    assert result.exit_code != 0
    assert "from_email" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_patch_message_requires_at_least_one_field(mock_build):
    ctx_obj, calls = _api([])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-message", "MSG1"])
    assert result.exit_code != 0
    assert "--subject" in result.output
    assert calls == []


@patch("klaviyo_cli.cli.build_context")
def test_patch_message_refuses_ab_test_variations(mock_build):
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message(
            variations=[_variation(vid="VA"), _variation(vid="VB")])),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-message", "MSG1", "--subject", "X"])
    assert result.exit_code != 0
    assert "A/B test" in result.output
    assert all(c[0] == "GET" for c in calls)


@patch("klaviyo_cli.cli.build_context")
def test_patch_message_refuses_sms(mock_build):
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message(
            variations=[_variation(channel="sms", body="hi")])),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["patch-message", "MSG1", "--subject", "X"])
    assert result.exit_code != 0
    assert "email only" in result.output


# ---------------------------------------------------------------------------
# clone-campaign
# ---------------------------------------------------------------------------


def _clone_routes():
    return [
        (("GET", "/api/campaign-messages/SRC1/"), _message(mid="SRC1", container="SRCCONT")),
        (("POST", "/api/campaign-clone/"), {"data": {"id": "NEWCONT", "type": "campaign"}}),
        (("GET", "/api/campaigns/NEWCONT/campaign-messages/"), {"data": [{"id": "MSGNEW"}]}),
        (("GET", "/api/campaign-messages/MSGNEW/"), _message(
            mid="MSGNEW", container="NEWCONT", name="[04-10-2026] Spring Sale",
            variations=[_variation(template_id="TPLNEW")])),
        (("GET", "/api/campaign-audiences/AUD1/"), _audience(("SEGA", "SEED"), ("SEGX",))),
    ]


@patch("klaviyo_cli.cli.build_context")
def test_clone_campaign_clones_the_container(mock_build):
    """campaign-clone takes the container ID, so a message ID is resolved first."""
    ctx_obj, calls = _api(_clone_routes())
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["clone-campaign", "SRC1", "--name", "[04-10-2026] Spring Sale"])
    assert result.exit_code == 0, result.output
    post = next(c for c in calls if c[0] == "POST")
    assert post[1] == "/api/campaign-clone/"
    assert post[2]["data"] == {"type": "campaign", "id": "SRCCONT",
                               "attributes": {"name": "[04-10-2026] Spring Sale"}}


@patch("klaviyo_cli.cli.build_context")
def test_clone_campaign_surfaces_ids_and_inherited_settings(mock_build):
    """Everything a clone silently inherits (subject, from address, audiences) has to
    be on screen, plus the ID the next steps need."""
    ctx_obj, _ = _api(_clone_routes())
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["clone-campaign", "SRC1", "--name", "[04-10-2026] Spring Sale"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Cloned SRC1 -> MSGNEW" in out
    for expected in ("NEWCONT", "TPLNEW", "Spring sale starts today",
                     "support@example.com", "SEGA", "SEGX", "set-campaign-html"):
        assert expected in out, expected


@patch("klaviyo_cli.cli.build_context")
def test_clone_campaign_requires_name(mock_build):
    ctx_obj, calls = _api([])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["clone-campaign", "SRC1"])
    assert result.exit_code != 0
    assert "--name" in result.output
    assert calls == []


# ---------------------------------------------------------------------------
# set-campaign-html
# ---------------------------------------------------------------------------

NEW_HTML = ('<html><body><img src="https://cdn.example.com/hero-v2.png">'
            '<a href="https://example.com/sale">Shop The Sale</a></body></html>')


def _html_file(tmp_path, html=NEW_HTML):
    f = tmp_path / "email.html"
    f.write_text(html)
    return str(f)


def _html_routes(live_html, live_tpl="TLIVE"):
    return [
        (("GET", "/api/campaign-messages/MSG1/"), _message(variations=[_variation(template_id="TOLD")])),
        (("POST", "/api/templates/"), {"data": {"id": "TTMP", "type": "template"}}),
        (("POST", "/api/campaign-message-assign-template/"), {"data": {"id": "VAR1"}}),
        (("GET", "/api/campaign-variations/VAR1/"), {"data": _variation(template_id=live_tpl)}),
        (("GET", f"/api/templates/{live_tpl}/"), {"data": {"attributes": {"html": live_html}}}),
        (("DELETE", "/api/templates/TTMP/"), {}),
    ]


@patch("klaviyo_cli.cli.build_context")
def test_set_campaign_html_creates_assigns_verifies_and_cleans_up(mock_build, tmp_path):
    """Assigning copies the template into the campaign, so: create a throwaway
    library template, assign it, verify the live creative, delete the throwaway."""
    ctx_obj, calls = _api(_html_routes("<head>tracking</head>" + NEW_HTML))
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["set-campaign-html", "MSG1", "--html", _html_file(tmp_path)])
    assert result.exit_code == 0, result.output
    create = next(c for c in calls if c[:2] == ("POST", "/api/templates/"))
    assert create[2]["data"]["attributes"]["html"] == NEW_HTML
    assert create[2]["data"]["attributes"]["editor_type"] == "CODE"
    assign = next(c for c in calls if c[1] == "/api/campaign-message-assign-template/")
    # At 2026-10-15 the assign endpoint takes the variation ID, not the message ID.
    assert assign[2]["data"]["id"] == "VAR1"
    assert assign[2]["data"]["relationships"]["template"]["data"]["id"] == "TTMP"
    assert calls[-1][:2] == ("DELETE", "/api/templates/TTMP/")
    assert "verified" in result.output.lower()


@patch("klaviyo_cli.cli.build_context")
def test_set_campaign_html_fails_when_live_creative_is_stale(mock_build, tmp_path):
    ctx_obj, _ = _api(_html_routes('<img src="https://cdn.example.com/hero-OLD.png">'))
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["set-campaign-html", "MSG1", "--html", _html_file(tmp_path)])
    assert result.exit_code != 0
    assert "hero-v2.png" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_set_campaign_html_fails_when_template_did_not_change(mock_build, tmp_path):
    ctx_obj, _ = _api(_html_routes(NEW_HTML, live_tpl="TOLD"))
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["set-campaign-html", "MSG1", "--html", _html_file(tmp_path)])
    assert result.exit_code != 0
    assert "old template TOLD" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_set_campaign_html_expect_and_forbid_checks(mock_build, tmp_path):
    live = NEW_HTML.replace("</body>", "<p>Shop Now</p></body>")
    ctx_obj, _ = _api(_html_routes(live))
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["set-campaign-html", "MSG1", "--html", _html_file(tmp_path),
                                       "--expect", "Shop The Sale", "--forbid", "Shop Now"])
    assert result.exit_code != 0
    assert "Shop Now" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_set_campaign_html_keep_template_skips_delete(mock_build, tmp_path):
    ctx_obj, calls = _api(_html_routes(NEW_HTML))
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["set-campaign-html", "MSG1", "--html", _html_file(tmp_path),
                                       "--keep-template"])
    assert result.exit_code == 0, result.output
    assert all(c[0] != "DELETE" for c in calls)
    assert "TTMP" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_set_campaign_html_deletes_throwaway_when_assign_fails(mock_build, tmp_path):
    routes = _html_routes(NEW_HTML)
    routes[2] = (("POST", "/api/campaign-message-assign-template/"), APIError("boom"))
    ctx_obj, calls = _api(routes)
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["set-campaign-html", "MSG1", "--html", _html_file(tmp_path)])
    assert result.exit_code != 0
    assert ("DELETE", "/api/templates/TTMP/") in [c[:2] for c in calls]


@patch("klaviyo_cli.cli.build_context")
def test_set_campaign_html_refuses_sms_before_creating_anything(mock_build, tmp_path):
    ctx_obj, calls = _api([(("GET", "/api/campaign-messages/MSG1/"),
                            _message(variations=[_variation(channel="sms", body="hi")]))])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["set-campaign-html", "MSG1", "--html", _html_file(tmp_path)])
    assert result.exit_code != 0
    assert "email only" in result.output
    assert all(c[0] == "GET" for c in calls)


@patch("klaviyo_cli.cli.build_context")
def test_search_query_with_apostrophe_uses_double_quotes(mock_build):
    ctx_obj, calls = _api([(("GET", "/api/campaign-messages/?"), _list_page([]))])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["search-campaigns", "Valentine's"])
    assert result.exit_code == 0, result.output
    assert "contains%28name%2C%22Valentine%27s%22%29" in calls[0][1]


# ---------------------------------------------------------------------------
# unschedule / reschedule
# ---------------------------------------------------------------------------


@patch("klaviyo_cli.cli.build_context")
def test_unschedule_reverts_never_cancels(mock_build):
    """'cancel' is final (a cancelled campaign can't be scheduled or deleted),
    so unschedule only ever sends 'revert'."""
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), [
            _message(status="scheduled", send_time="2027-03-01T15:00:00+00:00"),
            _message(status="draft")]),
        (("PATCH", "/api/campaign-message-schedule/MSG1/"), {}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["unschedule", "MSG1"])
    assert result.exit_code == 0, result.output
    patch_call = next(c for c in calls if c[0] == "PATCH")
    assert patch_call[2]["data"]["attributes"] == {"action": "revert"}
    assert "2027-03-01T15:00:00+00:00" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_unschedule_refuses_a_draft(mock_build):
    ctx_obj, calls = _api([(("GET", "/api/campaign-messages/MSG1/"), _message(status="draft"))])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["unschedule", "MSG1"])
    assert result.exit_code != 0
    assert "not Scheduled" in result.output
    assert all(c[0] == "GET" for c in calls)


@patch("klaviyo_cli.cli.build_context")
def test_reschedule_reverts_sets_time_schedules_and_verifies(mock_build):
    ctx_obj, calls = _api([
        (("GET", "/api/campaign-messages/MSG1/"), [
            _message(status="scheduled", send_time="2027-03-01T15:00:00+00:00"),
            _message(status="scheduled", send_time="2027-03-08T16:30:00+00:00")]),
        (("PATCH", "/api/campaign-message-schedule/MSG1/"), {}),
        (("PATCH", "/api/campaigns/MSG1/"), {}),
        (("POST", "/api/campaign-send-jobs/"), {}),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["reschedule", "MSG1", "--date", "03-08-2027",
                                       "--time", "11:30 AM EST"])
    assert result.exit_code == 0, result.output
    order = [(c[0], c[1]) for c in calls if c[0] != "GET"]
    assert order == [("PATCH", "/api/campaign-message-schedule/MSG1/"),
                     ("PATCH", "/api/campaigns/MSG1/"),
                     ("POST", "/api/campaign-send-jobs/")]
    date_patch = calls[[c[1] for c in calls].index("/api/campaigns/MSG1/")]
    assert date_patch[2]["data"]["attributes"]["send_strategy"]["datetime"] == "2027-03-08T11:30:00-05:00"
    assert date_patch[3] == LEGACY_REVISION
    assert "Now: 2027-03-08T16:30:00+00:00" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_reschedule_refuses_past_time_before_any_call(mock_build):
    ctx_obj, calls = _api([])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["reschedule", "MSG1", "--date", "01-01-2020", "--time", "9:00 AM EST"])
    assert result.exit_code != 0
    assert "in the past" in result.output
    assert calls == []


@patch("klaviyo_cli.cli.build_context")
def test_reschedule_says_what_state_it_left_when_scheduling_fails(mock_build):
    ctx_obj, _ = _api([
        (("GET", "/api/campaign-messages/MSG1/"), _message(status="scheduled",
                                                           send_time="2027-03-01T15:00:00+00:00")),
        (("PATCH", "/api/campaign-message-schedule/MSG1/"), {}),
        (("PATCH", "/api/campaigns/MSG1/"), {}),
        (("POST", "/api/campaign-send-jobs/"), APIError("audience is empty")),
    ])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["reschedule", "MSG1", "--date", "03-08-2027", "--time", "9:00 AM EST"])
    assert result.exit_code != 0
    assert "back in Draft with the new time" in result.output
    assert "klaviyo schedule MSG1" in result.output

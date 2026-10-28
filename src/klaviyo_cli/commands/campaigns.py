"""Campaign commands: list, search, patch, schedule, creative, performance.

Since API revision 2026-10-15 a campaign is a container of messages. Each
message has one audience and one or more variations, and the subject, sender
and template live on the variation. The ID a campaign has always had (in the
UI URL and in reporting) is now its MESSAGE ID; the container got a new one.
So these commands treat the message ID as "the campaign ID" and also accept a
container ID when the container holds a single message.

One thing has no home in the new model: the planned send date of a draft that
isn't scheduled yet. Setting it (patch-campaign --date, upload-sms) and sending
at it (schedule) still go through LEGACY_REVISION, where the same ID works.
"""

import re
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import click

from .._util import _parse_send_time, _resolve_date_range, output
from ..cli import main
from ..transport import APIError, AuthError, KLAVIYO_BASE

LEGACY_REVISION = "2026-07-15"

STATUSES = ("draft", "scheduled", "sending", "sent", "cancelled")


def _send_time_label(attrs: dict, unset: str = "not scheduled") -> str:
    """Scheduled campaigns carry send_time. An unscheduled draft keeps the date
    set by patch-campaign in send_strategy.datetime (send_time stays null until
    the campaign is scheduled), so fall back to that and say it's only planned."""
    if attrs.get("send_time"):
        return attrs["send_time"]
    planned = (attrs.get("send_strategy") or {}).get("datetime")
    if planned:
        return f"{planned} (planned, not scheduled yet)"
    return unset


def _audience_label(call, audience_id: str) -> str:
    """'ID (name)' for a segment or list ID. Campaign audiences don't say which
    type an ID is, so try segments, then lists; keep the bare ID if neither resolves."""
    for kind in ("segments", "lists"):
        try:
            resp = call("GET", f"/api/{kind}/{audience_id}/?fields[{kind[:-1]}]=name")
        except APIError:
            continue
        name = ((resp.get("data") or {}).get("attributes") or {}).get("name")
        if name:
            return f"{audience_id} ({name})"
    return audience_id


# ---------------------------------------------------------------------------
# message model helpers
# ---------------------------------------------------------------------------


def _not_found(e: Exception) -> bool:
    text = str(e).lower()
    return "not found" in text or "does not exist" in text


def _status_label(status: str | None) -> str:
    """'sent' -> 'Sent', matching what the CLI printed before 2026-10-15."""
    return (status or "?").capitalize()


def _variation_details(v: dict) -> dict:
    details = (((v.get("attributes") or {}).get("definition") or {}).get("details") or {})
    return {"id": v.get("id"), **details}


def _message_view(resp: dict) -> dict:
    """Flatten a campaign-message response (with campaign + variations included)."""
    data = resp.get("data") or {}
    attrs = data.get("attributes") or {}
    definition = attrs.get("definition") or {}
    rels = data.get("relationships") or {}
    included = resp.get("included") or []
    return {
        "id": data.get("id"),
        "campaign_id": ((rels.get("campaign") or {}).get("data") or {}).get("id"),
        "audience_id": ((rels.get("campaign-audience") or {}).get("data") or {}).get("id"),
        "name": definition.get("name"),
        "status": definition.get("status"),
        "send_time": (definition.get("send_options") or {}).get("send_time"),
        "send_strategy": (definition.get("send_options") or {}).get("send_strategy"),
        "created": attrs.get("created"),
        "variations": [_variation_details(i) for i in included
                       if i.get("type") == "campaign-variation"],
    }


def _get_message(call, message_id: str) -> dict:
    return _message_view(call(
        "GET", f"/api/campaign-messages/{message_id}/?include=campaign,campaign-variations"))


def _load(call, campaign_id: str, message_id: str | None = None) -> dict:
    """The message behind a campaign ID. Takes a message ID (the old campaign
    ID) or a container ID whose campaign holds one message; message_id picks
    one message when the container holds several."""
    if message_id:
        return _get_message(call, message_id)
    try:
        return _get_message(call, campaign_id)
    except APIError as e:
        if not _not_found(e):
            raise
    try:
        msgs = call("GET", f"/api/campaigns/{campaign_id}/campaign-messages/").get("data", [])
    except APIError as e:
        if _not_found(e):
            raise click.ClickException(f"No campaign or campaign message with ID {campaign_id}.")
        raise
    if not msgs:
        # An unknown container ID returns an empty list here, not a 404.
        raise click.ClickException(f"No campaign or campaign message with ID {campaign_id}.")
    if len(msgs) > 1:
        ids = ", ".join(m.get("id", "?") for m in msgs)
        raise click.ClickException(
            f"Campaign {campaign_id} has {len(msgs)} messages ({ids}); "
            "pass one of those IDs or --message-id."
        )
    return _get_message(call, msgs[0]["id"])


def _only_variation(view: dict) -> dict:
    variations = view["variations"]
    if not variations:
        raise click.ClickException(f"Campaign {view['id']} has no content variation.")
    if len(variations) > 1:
        raise click.ClickException(
            f"Campaign {view['id']} has {len(variations)} content variations (an A/B test); "
            "edit it in the Klaviyo UI."
        )
    return variations[0]


def _audience(call, view: dict) -> dict:
    """{'included': [...], 'excluded': [...]} for the message's audience."""
    if view.get("audience_id"):
        path = f"/api/campaign-audiences/{view['audience_id']}/"
    else:
        path = f"/api/campaign-messages/{view['id']}/campaign-audience/"
    data = call("GET", path).get("data") or {}
    definition = ((data.get("attributes") or {}).get("definition") or {})
    return {"id": data.get("id"), "included": definition.get("included") or [],
            "excluded": definition.get("excluded") or []}


def _legacy_send_strategy(call, message_id: str) -> dict:
    """An unscheduled draft's planned date only exists at LEGACY_REVISION."""
    try:
        resp = call("GET", f"/api/campaigns/{message_id}/?fields[campaign]=send_strategy",
                    revision=LEGACY_REVISION)
    except APIError:
        return {}
    return ((resp.get("data") or {}).get("attributes") or {}).get("send_strategy") or {}


def _row(message: dict, variations: dict) -> dict:
    """One list row, in the shape these commands' --json printed before 2026-10-15
    (id + attributes.name/status/send_time/created_at), plus channel and subject."""
    attrs = message.get("attributes") or {}
    definition = attrs.get("definition") or {}
    rels = message.get("relationships") or {}
    var_ids = [v.get("id") for v in ((rels.get("campaign-variations") or {}).get("data") or [])]
    first = next((variations[v] for v in var_ids if v in variations), {})
    return {
        "id": message.get("id"),
        "type": "campaign",
        "attributes": {
            "name": definition.get("name"),
            "status": _status_label(definition.get("status")),
            "send_time": (definition.get("send_options") or {}).get("send_time"),
            "created_at": attrs.get("created"),
            "channel": first.get("channel"),
            "subject": first.get("subject"),
        },
        "campaign_id": ((rels.get("campaign") or {}).get("data") or {}).get("id"),
        "audience_id": ((rels.get("campaign-audience") or {}).get("data") or {}).get("id"),
    }


def _list_messages(call, filters: list[str], channel: str | None = None,
                   limit: int | None = None) -> list[dict]:
    """Campaign messages matching the filters, newest first, as list rows.
    Channel lives on the variations, so it's filtered here, not by the API."""
    params = ["sort=-created", "page[size]=100", "include=campaign-variations",
              "fields[campaign-variation]=definition"]
    if filters:
        filt = filters[0] if len(filters) == 1 else "and(" + ",".join(filters) + ")"
        params.insert(0, f"filter={urllib.parse.quote(filt)}")
    path = "/api/campaign-messages/?" + "&".join(params)
    rows: list[dict] = []
    while path and (limit is None or len(rows) < limit):
        data = call("GET", path)
        variations = {v.get("id"): _variation_details(v) for v in data.get("included") or []}
        for m in data.get("data", []):
            row = _row(m, variations)
            if channel is None or row["attributes"]["channel"] == channel:
                rows.append(row)
        next_link = (data.get("links") or {}).get("next")
        path = next_link.replace(KLAVIYO_BASE, "") if next_link else None
    return rows[:limit] if limit is not None else rows


def _status_filter(status: str) -> str:
    """Accept the old capitalized statuses ('Sent', 'Cancelled: No Recipients')."""
    value = status.split(":")[0].strip().lower()
    if value not in STATUSES:
        raise click.UsageError(f"--status must be one of: {', '.join(STATUSES)}")
    return f"equals(status,'{value}')"


# ---------------------------------------------------------------------------
# list-drafts
# ---------------------------------------------------------------------------


@main.command("list-drafts")
@click.pass_context
def list_drafts(ctx):
    """List draft email campaigns for a client."""
    use_json = ctx.obj["json"]
    try:
        campaigns = _list_messages(ctx.obj["call"], ["equals(status,'draft')"], "email")

        if use_json:
            output({"data": campaigns}, use_json=True)
        else:
            print(f"Draft Campaigns for {ctx.obj['label']}:")
            if not campaigns:
                print("  (none)")
                return
            for item in campaigns:
                attrs = item["attributes"]
                created = (attrs.get("created_at") or "").split("T")[0]
                print(f"  [{item['id']}] \"{attrs.get('name') or '?'}\" — created {created}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# get-campaign
# ---------------------------------------------------------------------------


@main.command("get-campaign")
@click.argument("campaign_id")
@click.option("--names", "show_names", is_flag=True,
              help="Resolve audience IDs to segment/list names (one extra call per ID)")
@click.pass_context
def get_campaign(ctx, campaign_id, show_names):
    """Show details for a specific campaign."""
    use_json = ctx.obj["json"]
    try:
        call = ctx.obj["call"]
        view = _load(call, campaign_id)
        audience = _audience(call, view)
        strategy = {}
        if view["status"] == "draft" and not view["send_time"]:
            strategy = _legacy_send_strategy(call, view["id"])
        variation = view["variations"][0] if view["variations"] else {}
        # Subject + preview text let the scheduling workflow catch clone-leftover
        # subject lines (an email whose SL belongs to the cloned-from campaign).
        subject = variation.get("subject")
        preview = variation.get("preview_text")
        attrs = {"send_time": view["send_time"], "send_strategy": strategy}

        if use_json:
            output({"data": {"id": view["id"], "type": "campaign", "attributes": {
                "name": view["name"],
                "status": _status_label(view["status"]),
                "send_time": view["send_time"],
                "created_at": view["created"],
                "audiences": {"included": audience["included"],
                              "excluded": audience["excluded"]},
                "send_strategy": strategy or {"method": view["send_strategy"], "datetime": None},
                "subject": subject,
                "preview_text": preview,
            }}, "campaign_id": view["campaign_id"], "variations": view["variations"]},
                use_json=True)
            return
        included, excluded = audience["included"], audience["excluded"]
        if show_names:
            included = [_audience_label(call, a) for a in included]
            excluded = [_audience_label(call, a) for a in excluded]
        print(f"Campaign: {view['name'] or '?'}")
        print(f"  ID: {view['id']}")
        if campaign_id != view["id"]:
            print(f"  (container campaign {campaign_id})")
        print(f"  Status: {_status_label(view['status'])}")
        print(f"  Subject: {subject or '(none)'}")
        print(f"  Preview: {preview or '(none)'}")
        print(f"  Send Time: {_send_time_label(attrs)}")
        print(f"  Created: {(view['created'] or '').split('T')[0]}")
        print("  Audiences:")
        print(f"    Include: {', '.join(included) or '(none)'}")
        print(f"    Exclude: {', '.join(excluded) or '(none)'}")
        print(f"  Send Strategy: {strategy.get('method') or view['send_strategy'] or 'N/A'}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# search-campaigns
# ---------------------------------------------------------------------------


@main.command("search-campaigns")
@click.argument("query")
@click.pass_context
def search_campaigns(ctx, query):
    """Search campaigns by name."""
    use_json = ctx.obj["json"]
    try:
        # Filter strings take either quote; use double quotes for "Valentine's".
        quoted = f'"{query}"' if "'" in query else f"'{query}'"
        filters = [f"contains(name,{quoted})"] if query else []
        campaigns = _list_messages(ctx.obj["call"], filters, "email")

        if use_json:
            output({"data": campaigns}, use_json=True)
        else:
            print(f"Campaigns matching '{query}' for {ctx.obj['label']} ({len(campaigns)} results):")
            if not campaigns:
                print("  (none)")
                return
            for item in campaigns:
                attrs = item["attributes"]
                send_time = attrs.get("send_time")
                if send_time:
                    date_part = f"(send: {send_time.split('T')[0]})"
                else:
                    created = (attrs.get("created_at") or "").split("T")[0]
                    date_part = f"(created: {created})"
                print(f"  [{item['id']}] \"{attrs.get('name') or '?'}\" — {attrs['status']} {date_part}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# patch-campaign
# ---------------------------------------------------------------------------


@main.command("patch-campaign")
@click.argument("campaign_id")
@click.option("--date", "date_val", default=None, help="Send date (MM-DD-YYYY)")
@click.option("--time", "time_val", default=None, help="Send time (e.g. '3:00 PM EST')")
@click.option("--include", "include_ids", default=None, help="Comma-separated list/segment IDs to include")
@click.option("--exclude", "exclude_ids", default=None, help="Comma-separated list/segment IDs to exclude")
@click.pass_context
def patch_campaign(ctx, campaign_id, date_val, time_val, include_ids, exclude_ids):
    """Update campaign send time and/or audiences."""
    use_json = ctx.obj["json"]
    if date_val and not time_val:
        raise click.UsageError("--date requires --time as well.")
    if time_val and not date_val:
        raise click.UsageError("--time requires --date as well.")
    set_audiences = include_ids is not None or exclude_ids is not None
    if not date_val and not set_audiences:
        raise click.UsageError("Provide at least one of: --date/--time, --include, --exclude")
    try:
        call = ctx.obj["call"]
        view = _load(call, campaign_id)
        result: dict = {"id": view["id"]}

        if set_audiences:
            included = [i.strip() for i in (include_ids or "").split(",") if i.strip()]
            excluded = [i.strip() for i in (exclude_ids or "").split(",") if i.strip()]
            aid = view["audience_id"] or _audience(call, view)["id"]
            resp = call("PATCH", f"/api/campaign-audiences/{aid}/", body={"data": {
                "type": "campaign-audience", "id": aid,
                "attributes": {"definition": {"included": included, "excluded": excluded}},
            }})
            result["audience"] = resp.get("data")

        attrs = {"send_time": view["send_time"]}
        if date_val:
            # A draft's planned date has no field at 2026-10-15 (a send_time PATCH
            # on a draft is silently dropped), so set it where it still exists.
            resp = call("PATCH", f"/api/campaigns/{view['id']}/", body={"data": {
                "type": "campaign", "id": view["id"],
                "attributes": {"send_strategy": {
                    "method": "static",
                    "datetime": _parse_send_time(date_val, time_val),
                    "options": {"is_local": False},
                }},
            }}, revision=LEGACY_REVISION)
            attrs = (resp.get("data") or {}).get("attributes") or {}
            result["campaign"] = resp.get("data")

        if use_json:
            output(result, use_json=True)
        else:
            print(f"Updated campaign: {view['name'] or view['id']}")
            print(f"  Send Time: {_send_time_label(attrs, unset='not set')}")
            if set_audiences:
                print(f"  Include: {include_ids or '(none)'}")
                print(f"  Exclude: {exclude_ids or '(none)'}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------


@main.command("schedule")
@click.argument("campaign_id")
@click.pass_context
def schedule(ctx, campaign_id):
    """Schedule a campaign for sending."""
    use_json = ctx.obj["json"]
    try:
        call = ctx.obj["call"]
        mid = _load(call, campaign_id)["id"]
        # Sends at the draft's planned date, which only LEGACY_REVISION knows.
        payload = {"data": {"type": "campaign-send-job", "id": mid}}
        data = call("POST", "/api/campaign-send-jobs/", body=payload, revision=LEGACY_REVISION)

        if use_json:
            output(data, use_json=True)
        else:
            errors = data.get("errors")
            if errors:
                raise click.ClickException(
                    f"Error scheduling campaign: {errors[0].get('detail', str(errors[0]))}"
                )
            print(f"Campaign {mid} scheduled for sending.")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# unschedule / reschedule
# ---------------------------------------------------------------------------


def _revert_to_draft(call, message_id: str) -> None:
    # "revert" puts a scheduled campaign back to Draft and keeps its planned
    # date. The other action, "cancel", is final: a cancelled campaign can
    # never be scheduled again, so this CLI doesn't offer it.
    call("PATCH", f"/api/campaign-message-schedule/{message_id}/", body={"data": {
        "type": "campaign-message-schedule", "id": message_id,
        "attributes": {"action": "revert"},
    }})


@main.command("unschedule")
@click.argument("campaign_id")
@click.pass_context
def unschedule(ctx, campaign_id):
    """Take a scheduled campaign back to Draft. Its planned send time is kept."""
    use_json = ctx.obj["json"]
    try:
        call = ctx.obj["call"]
        view = _load(call, campaign_id)
        if view["status"] != "scheduled":
            raise click.ClickException(
                f"Campaign {view['id']} is {_status_label(view['status'])}, not Scheduled.")
        _revert_to_draft(call, view["id"])
        after = _get_message(call, view["id"])
        if after["status"] != "draft":
            raise click.ClickException(
                f"Klaviyo accepted the request but the campaign is {_status_label(after['status'])}. "
                "Check it in the Klaviyo UI.")
        if use_json:
            output({"id": view["id"], "status": "Draft", "planned_send_time": view["send_time"]},
                   use_json=True)
        else:
            print(f"Unscheduled {view['name'] or view['id']} (now Draft).")
            print(f"  Planned send time kept: {view['send_time']}")
            print(f"  To send at that time again: klaviyo schedule {view['id']}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


@main.command("reschedule")
@click.argument("campaign_id")
@click.option("--date", "date_val", required=True, help="New send date (MM-DD-YYYY)")
@click.option("--time", "time_val", required=True, help="New send time (e.g. '3:00 PM EST')")
@click.pass_context
def reschedule(ctx, campaign_id, date_val, time_val):
    """Move a scheduled (or draft) campaign to a new send time and schedule it.

    Klaviyo can't move a scheduled send directly: this takes it back to Draft,
    sets the new time, schedules it again, and checks the result.
    """
    use_json = ctx.obj["json"]
    new_time = _parse_send_time(date_val, time_val)
    if datetime.fromisoformat(new_time) <= datetime.now(ZoneInfo("UTC")):
        raise click.UsageError(f"{new_time} is in the past; scheduling it would send right away.")
    try:
        call = ctx.obj["call"]
        view = _load(call, campaign_id)
        if view["status"] not in ("scheduled", "draft"):
            raise click.ClickException(
                f"Campaign {view['id']} is {_status_label(view['status'])}; only Scheduled or "
                "Draft campaigns can be rescheduled.")
        old_time = view["send_time"]
        if view["status"] == "scheduled":
            _revert_to_draft(call, view["id"])
        call("PATCH", f"/api/campaigns/{view['id']}/", body={"data": {
            "type": "campaign", "id": view["id"],
            "attributes": {"send_strategy": {"method": "static", "datetime": new_time,
                                             "options": {"is_local": False}}},
        }}, revision=LEGACY_REVISION)
        try:
            call("POST", "/api/campaign-send-jobs/",
                 body={"data": {"type": "campaign-send-job", "id": view["id"]}},
                 revision=LEGACY_REVISION)
        except APIError as e:
            raise click.ClickException(
                f"Campaign {view['id']} is back in Draft with the new time, but scheduling it "
                f"failed: {e}. Fix that, then run: klaviyo schedule {view['id']}")
        after = _get_message(call, view["id"])
        if after["status"] != "scheduled":
            raise click.ClickException(
                f"Campaign {view['id']} is {_status_label(after['status'])} after rescheduling. "
                "Check it in the Klaviyo UI.")
        if use_json:
            output({"id": view["id"], "status": "Scheduled", "old_send_time": old_time,
                    "send_time": after["send_time"]}, use_json=True)
        else:
            print(f"Rescheduled {view['name'] or view['id']} (verified).")
            print(f"  Was: {old_time or 'not scheduled'}")
            print(f"  Now: {after['send_time']}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# campaign-performance
# ---------------------------------------------------------------------------


@main.command("campaign-performance")
@click.option("--days", default=90, help="Days to look back")
@click.option("--since", help="Start date YYYY-MM-DD (overrides --days)")
@click.option("--until", help="End date YYYY-MM-DD (defaults to today)")
@click.option("--detail", is_flag=True, help="Show per-campaign breakdown with subject lines")
@click.option("--top", "top_n", default=None, type=int, help="Show only top N campaigns by revenue (implies --detail)")
@click.option("--sort", "sort_by", default="revenue", type=click.Choice(["revenue", "date", "open_rate", "click_rate"]), help="Sort detail rows")
@click.pass_context
def campaign_performance(ctx, days, since, until, detail, top_n, sort_by):
    """Show campaign revenue and engagement metrics."""
    use_json = ctx.obj["json"]
    if top_n:
        detail = True
    try:
        start, end, label = _resolve_date_range(days, since, until)

        # Find Placed Order metric
        metrics_resp = ctx.obj["call"]("GET", "/api/metrics/")
        conversion_id = None
        for m in metrics_resp.get("data", []):
            name = m.get("attributes", {}).get("name", "").lower()
            integration = (m.get("attributes", {}).get("integration") or {}).get("name", "")
            if name == "placed order" and integration == "Shopify":
                conversion_id = m["id"]
                break
        if not conversion_id:
            for m in metrics_resp.get("data", []):
                if m.get("attributes", {}).get("name", "").lower() == "placed order":
                    conversion_id = m["id"]
                    break

        body = {
            "data": {
                "type": "campaign-values-report",
                "attributes": {
                    "timeframe": {
                        "start": start.strftime("%Y-%m-%dT00:00:00"),
                        "end": end.strftime("%Y-%m-%dT23:59:59"),
                    },
                    "statistics": [
                        "conversion_value", "revenue_per_recipient",
                        "delivered", "opens_unique", "clicks_unique",
                        "unsubscribes", "spam_complaints",
                    ],
                },
            }
        }
        if conversion_id:
            body["data"]["attributes"]["conversion_metric_id"] = conversion_id

        data = ctx.obj["call"]("POST", "/api/campaign-values-reports/", body=body)
        results = data.get("data", {}).get("attributes", {}).get("results", [])

        if use_json:
            output({"results": results}, use_json=True)
        elif detail:
            _print_campaign_detail(ctx, results, sort_by, top_n)
        else:
            total_rev = total_del = total_opens = total_clicks = total_unsubs = total_spam = 0
            for r in results:
                s = r["statistics"]
                total_rev += s.get("conversion_value", 0)
                total_del += int(s.get("delivered", 0))
                total_opens += int(s.get("opens_unique", 0))
                total_clicks += int(s.get("clicks_unique", 0))
                total_unsubs += int(s.get("unsubscribes", 0))
                total_spam += int(s.get("spam_complaints", 0))

            orate = (total_opens / total_del * 100) if total_del > 0 else 0
            crate = (total_clicks / total_del * 100) if total_del > 0 else 0
            spam_rate = (total_spam / total_del * 100) if total_del > 0 else 0

            print(f"Campaign Performance for {ctx.obj['label']} ({label}):\n")
            print(f"  Campaigns sent:    {len(results)}")
            print(f"  Total Revenue:     ${total_rev:,.2f}")
            print(f"  Total Delivered:   {total_del:,}")
            print(f"  Open Rate:         {orate:.1f}%")
            print(f"  Click Rate:        {crate:.1f}%")
            print(f"  Unsubscribes:      {total_unsubs:,}")
            print(f"  Spam Complaints:   {total_spam:,}")
            print(f"  Spam Rate:         {spam_rate:.3f}%")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


def _print_campaign_detail(ctx, results, sort_by, top_n):
    """Print per-campaign breakdown with subject lines."""
    if not results:
        print("No campaigns found.")
        return

    # Collect unique campaign IDs
    campaign_ids = set()
    for r in results:
        cid = r.get("groupings", {}).get("campaign_id", "")
        if cid:
            campaign_ids.add(cid)

    # Fetch each campaign's name, send date and subject. Report groupings key
    # on the message ID, which is what _load takes.
    campaign_info = {}
    for cid in campaign_ids:
        try:
            view = _load(ctx.obj["call"], cid)
            variation = view["variations"][0] if view["variations"] else {}
            campaign_info[cid] = {
                "name": view["name"] or "",
                "send_date": (view["send_time"] or "").split("T")[0],
                "subject": variation.get("subject") or "",
                "preview": variation.get("preview_text") or "",
            }
        except Exception:
            campaign_info[cid] = {"name": "", "send_date": "", "subject": "", "preview": ""}

    # Build rows
    rows = []
    for r in results:
        s = r["statistics"]
        g = r.get("groupings", {})
        cid = g.get("campaign_id", "")
        delivered = int(s.get("delivered", 0))
        opens = int(s.get("opens_unique", 0))
        clicks = int(s.get("clicks_unique", 0))
        rev = s.get("conversion_value", 0)
        unsubs = int(s.get("unsubscribes", 0))
        open_rate = (opens / delivered * 100) if delivered > 0 else 0
        click_rate = (clicks / delivered * 100) if delivered > 0 else 0
        info = campaign_info.get(cid, {})
        rows.append({
            "campaign_id": cid,
            "name": info.get("name", ""),
            "send_date": info.get("send_date", ""),
            "subject": info.get("subject", ""),
            "preview": info.get("preview", ""),
            "delivered": delivered,
            "opens": opens,
            "open_rate": open_rate,
            "clicks": clicks,
            "click_rate": click_rate,
            "revenue": rev,
            "unsubs": unsubs,
        })

    # Sort
    sort_keys = {
        "revenue": lambda r: r["revenue"],
        "date": lambda r: r["send_date"],
        "open_rate": lambda r: r["open_rate"],
        "click_rate": lambda r: r["click_rate"],
    }
    rows.sort(key=sort_keys.get(sort_by, sort_keys["revenue"]), reverse=True)

    if top_n:
        rows = rows[:top_n]

    print(f"Campaign Detail for {ctx.obj['label']} ({len(rows)} campaigns):\n")
    for i, row in enumerate(rows, 1):
        subj = row["subject"] or "(no subject)"
        preview = row["preview"]
        print(f"  {i}. {subj}")
        if preview:
            print(f"     Preview: {preview}")
        print(f"     Sent: {row['send_date']}  |  Delivered: {row['delivered']:,}")
        print(f"     Opens: {row['open_rate']:.1f}%  |  Clicks: {row['click_rate']:.2f}%  |  Revenue: ${row['revenue']:,.2f}")
        print(f"     Unsubs: {row['unsubs']:,}  |  ID: {row['campaign_id']}")
        print()



# ---------------------------------------------------------------------------
# get-creative
# ---------------------------------------------------------------------------


@main.command("get-creative")
@click.argument("campaign_id")
@click.option("--html", "show_html", is_flag=True, help="Show full HTML instead of the text version")
@click.option("--grep", "grep_term", default=None, help="Only show body lines containing this term (case-insensitive)")
@click.pass_context
def get_creative(ctx, campaign_id, show_html, grep_term):
    """Dump a campaign's creative (subject + text/HTML) via its template."""
    use_json = ctx.obj["json"]
    try:
        call = ctx.obj["call"]
        view = _load(call, campaign_id)
        results = []
        for v in view["variations"]:
            html = text = None
            if v.get("template_id"):
                tattrs = call("GET", f"/api/templates/{v['template_id']}/").get("data", {}).get("attributes", {})
                html = tattrs.get("html")
                text = tattrs.get("text")
            results.append({
                "message_id": view["id"],
                "variation_id": v.get("id"),
                "channel": v.get("channel"),
                "subject": v.get("subject"),
                "preview": v.get("preview_text"),
                "template_id": v.get("template_id"),
                "html": html, "text": text, "body": v.get("body"),
            })

        if use_json:
            output({"campaign_id": view["id"], "messages": results}, use_json=True)
            return
        if not results:
            print("(no content on this campaign)")
            return
        for r in results:
            print(f"=== message {r['message_id']} ({r.get('channel') or '?'}) ===")
            if r.get("subject") is not None:
                print(f"  Subject: {r['subject']}")
            if r.get("preview"):
                print(f"  Preview: {r['preview']}")
            if r.get("template_id"):
                print(f"  Template: {r['template_id']}")
            if show_html:
                content, label = (r.get("html") or ""), "HTML"
            else:
                content = r.get("text") or r.get("body") or ""
                label = "Text"
                if not content and r.get("html"):
                    content = "(no plaintext version — use --html to see HTML)"
            lines = content.splitlines() if content else []
            if grep_term:
                gt = grep_term.lower()
                lines = [ln for ln in lines if gt in ln.lower()]
            print(f"  {label}:")
            for ln in lines:
                print(f"    {ln}")
            if grep_term and not lines:
                print(f"    (no lines matching '{grep_term}')")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# clone-campaign
# ---------------------------------------------------------------------------


@main.command("clone-campaign")
@click.argument("campaign_id")
@click.option("--name", "new_name", required=True, help="Name for the new draft")
@click.pass_context
def clone_campaign(ctx, campaign_id, new_name):
    """Duplicate a campaign into a new draft and show what it inherited.

    A clone carries the source's subject line, from address, and audiences
    along with the creative -- only the name changes. All three are printed so
    they can be checked against the plan. Edit the creative with
    set-campaign-html.
    """
    use_json = ctx.obj["json"]
    try:
        call = ctx.obj["call"]
        source = _load(call, campaign_id)
        # The clone endpoint takes the container ID, not the message ID.
        payload = {"data": {"type": "campaign", "id": source["campaign_id"],
                            "attributes": {"name": new_name}}}
        created = call("POST", "/api/campaign-clone/", body=payload).get("data") or {}
        container = created.get("id")
        msgs = call("GET", f"/api/campaigns/{container}/campaign-messages/").get("data", [])
        clones = []
        for m in msgs:
            view = _get_message(call, m["id"])
            view["audience"] = _audience(call, view)
            clones.append(view)

        if use_json:
            output({"campaign": created, "messages": clones}, use_json=True)
            return
        new_ids = ", ".join(c["id"] for c in clones) or "(no messages)"
        print(f"Cloned {campaign_id} -> {new_ids}")
        print(f"  Name: {new_name}")
        print(f"  Container campaign: {container}")
        print("  Inherited from the source (check against the plan):")
        for c in clones:
            print(f"    Campaign {c['id']} ({_status_label(c['status'])}):")
            print(f"      Include: {', '.join(c['audience']['included']) or '(none)'}")
            print(f"      Exclude: {', '.join(c['audience']['excluded']) or '(none)'}")
            for v in c["variations"]:
                print(f"      Template: {v.get('template_id') or '(none)'}")
                print(f"      Subject: {v.get('subject') or '(none)'}")
                print(f"      From: {v.get('from_email') or '(none)'}")
        print("  Edit the creative with set-campaign-html.")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# patch-message
# ---------------------------------------------------------------------------


@main.command("patch-message")
@click.argument("campaign_id")
@click.option("--subject", default=None, help="Subject line")
@click.option("--preview", default=None, help="Preview text")
@click.option("--from-email", default=None, help="From address")
@click.option("--from-label", default=None, help="From name")
@click.option("--reply-to", default=None, help="Reply-to address")
@click.option("--message-id", default=None, help="Which message, if the campaign has more than one")
@click.pass_context
def patch_message(ctx, campaign_id, subject, preview, from_email, from_label, reply_to, message_id):
    """Change an email campaign's subject, preview text, or sender, keeping every other field.

    The content lives on the message's variation. This patches only the fields
    you pass, then re-reads the variation and fails if anything didn't stick
    or changed that you didn't ask for.
    """
    use_json = ctx.obj["json"]
    changes = {k: v for k, v in {
        "subject": subject, "preview_text": preview, "from_email": from_email,
        "from_label": from_label, "reply_to_email": reply_to,
    }.items() if v is not None}
    if not changes:
        raise click.UsageError(
            "Provide at least one of: --subject, --preview, --from-email, --from-label, --reply-to"
        )
    try:
        call = ctx.obj["call"]
        view = _load(call, campaign_id, message_id)
        before = _only_variation(view)
        vid = before["id"]
        if before.get("channel") != "email":
            raise click.ClickException(
                f"Campaign {view['id']} is {before.get('channel') or 'an unknown channel'}; "
                "patch-message handles email only."
            )
        call("PATCH", f"/api/campaign-variations/{vid}/", body={"data": {
            "type": "campaign-variation", "id": vid,
            "attributes": {"definition": {"details": {"channel": "email", **changes}}},
        }})

        # A 2xx isn't proof: re-read and compare.
        after = _variation_details(call("GET", f"/api/campaign-variations/{vid}/").get("data") or {})
        not_applied = [k for k, v in changes.items() if after.get(k) != v]
        if not_applied:
            raise click.ClickException(
                "Klaviyo accepted the update but the campaign still shows the old "
                f"{', '.join(not_applied)}. Check the campaign in the Klaviyo UI."
            )
        collateral = [k for k, v in before.items() if k not in changes and after.get(k) != v]
        if collateral:
            raise click.ClickException(
                f"The update changed fields you didn't ask to change: {', '.join(collateral)}. "
                "Check the campaign in the Klaviyo UI."
            )

        if use_json:
            output({"campaign_id": view["id"], "variation_id": vid,
                    "before": before, "after": after}, use_json=True)
        else:
            print(f"Updated campaign {view['id']} (verified):")
            for k in changes:
                print(f"  {k}: {before.get(k)!r} -> {after.get(k)!r}")
            if "from_email" not in changes:
                print(f"  from_email unchanged: {after.get('from_email')}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# set-campaign-html
# ---------------------------------------------------------------------------

_IMG_SRC = re.compile(r'<img[^>]*\ssrc="([^"]+)"', re.IGNORECASE)


@main.command("set-campaign-html")
@click.argument("campaign_id")
@click.option("--html", "html_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="HTML file to use as the campaign's creative")
@click.option("--expect", multiple=True,
              help="Text that must appear in the live creative afterwards (repeatable)")
@click.option("--forbid", multiple=True,
              help="Text that must NOT appear in the live creative afterwards (repeatable)")
@click.option("--message-id", default=None, help="Which message, if the campaign has more than one")
@click.option("--keep-template", is_flag=True,
              help="Keep the intermediate library template instead of deleting it")
@click.pass_context
def set_campaign_html(ctx, campaign_id, html_path, expect, forbid, message_id, keep_template):
    """Replace an email campaign's HTML creative and verify it took.

    Assigning a template to a campaign copies its HTML into the campaign, so
    later edits to that template never reach it. This creates a throwaway
    library template from the file, assigns it, re-reads the live creative and
    checks it (every <img> URL from the file must be there, plus any --expect /
    --forbid text), then deletes the throwaway unless --keep-template.
    """
    use_json = ctx.obj["json"]
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    try:
        call = ctx.obj["call"]
        view = _load(call, campaign_id, message_id)
        variation = _only_variation(view)
        vid = variation["id"]
        old_tpl = variation.get("template_id")
        if variation.get("channel") != "email":
            raise click.ClickException(
                f"Campaign {view['id']} is {variation.get('channel') or 'an unknown channel'}; "
                "set-campaign-html handles email only."
            )

        created = call("POST", "/api/templates/", body={"data": {"type": "template", "attributes": {
            "name": f"klaviyo-cli set-campaign-html {view['id']}",
            "editor_type": "CODE",
            "html": html,
        }}})
        tmp_id = (created.get("data") or {}).get("id")
        try:
            # Typed "campaign-message", but at 2026-10-15 it takes the VARIATION ID
            # (a message ID fails with "A message with id ... does not exist").
            call("POST", "/api/campaign-message-assign-template/", body={"data": {
                "type": "campaign-message", "id": vid,
                "relationships": {"template": {"data": {"type": "template", "id": tmp_id}}},
            }})

            live_tpl = _variation_details(
                call("GET", f"/api/campaign-variations/{vid}/").get("data") or {}).get("template_id")
            live_html = ((call("GET", f"/api/templates/{live_tpl}/").get("data") or {})
                         .get("attributes") or {}).get("html") or ""

            images = _IMG_SRC.findall(html)
            problems = []
            if live_tpl == old_tpl:
                problems.append(f"campaign still points at its old template {old_tpl}")
            missing = [u for u in images if u not in live_html]
            if missing:
                problems.append("image URLs from your file are missing from the live creative: "
                                + ", ".join(missing))
            problems += [f"expected text not found: {t!r}" for t in expect if t not in live_html]
            problems += [f"forbidden text still present: {t!r}" for t in forbid if t in live_html]
        finally:
            # Delete the throwaway even when assign or verify fails.
            if tmp_id and not keep_template:
                call("DELETE", f"/api/templates/{tmp_id}/")
        if problems:
            raise click.ClickException("Creative did not verify:\n  " + "\n  ".join(problems))

        if use_json:
            output({"campaign_id": view["id"], "variation_id": vid, "live_template_id": live_tpl,
                    "intermediate_template_id": tmp_id, "kept_intermediate": keep_template,
                    "checked": {"images": len(images), "expect": list(expect),
                                "forbid": list(forbid)}}, use_json=True)
        else:
            print(f"Creative replaced on campaign {view['id']} (verified).")
            print(f"  Live template: {live_tpl}")
            print(f"  Checked: {len(images)} image URL(s), {len(expect)} --expect, "
                  f"{len(forbid)} --forbid")
            if keep_template:
                print(f"  Kept intermediate template {tmp_id}")
            else:
                print(f"  Removed intermediate template {tmp_id}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# list-campaigns
# ---------------------------------------------------------------------------


@main.command("list-campaigns")
@click.option("--status", default=None, help="Filter by status (draft, scheduled, sending, sent, cancelled)")
@click.option("--channel", default="email", show_default=True,
              type=click.Choice(["email", "sms"]), help="Message channel")
@click.option("--days", default=None, type=int, help="Created within the last N days")
@click.option("--since", default=None, help="Created on/after YYYY-MM-DD")
@click.option("--until", default=None, help="Created on/before YYYY-MM-DD")
@click.option("--limit", default=25, show_default=True, help="Max campaigns to show")
@click.pass_context
def list_campaigns(ctx, status, channel, days, since, until, limit):
    """List/filter campaigns by status, channel, and date (beyond name search)."""
    use_json = ctx.obj["json"]
    filters = [_status_filter(status)] if status else []
    if days is not None:
        since_dt = datetime.now(ZoneInfo("UTC")) - timedelta(days=days)
        filters.append(f"greater-than(created,{since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')})")
    if since:
        filters.append(f"greater-than(created,{since}T00:00:00Z)")
    if until:
        filters.append(f"less-than(created,{until}T23:59:59Z)")
    try:
        campaigns = _list_messages(ctx.obj["call"], filters, channel, limit)

        if use_json:
            output({"data": campaigns}, use_json=True)
        else:
            scope = f"channel={channel}" + (f", status={status}" if status else "")
            print(f"Campaigns for {ctx.obj['label']} ({scope}) — {len(campaigns)} shown:")
            for c in campaigns:
                a = c["attributes"]
                st = a.get("send_time")
                when = (f"send {st.split('T')[0]}" if st
                        else f"created {(a.get('created_at') or '').split('T')[0]}")
                print(f"  [{c['id']}] {a['status']:10} {a.get('name') or '?'}  ({when})")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

"""Campaign commands: list, search, patch, schedule, creative, performance."""

import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import click

from .._util import _parse_send_time, _resolve_date_range, output
from ..cli import main
from ..transport import APIError, AuthError, KLAVIYO_BASE

# ---------------------------------------------------------------------------
# list-drafts
# ---------------------------------------------------------------------------


@main.command("list-drafts")
@click.pass_context
def list_drafts(ctx):
    """List draft email campaigns for a client."""
    use_json = ctx.obj["json"]
    try:
        filter_val = urllib.parse.quote(
            "and(equals(messages.channel,'email'),equals(status,'Draft'))"
        )
        fields = "fields[campaign]=name,status,created_at,updated_at,send_time"
        path = f"/api/campaigns/?filter={filter_val}&{fields}"
        data = ctx.obj["call"]("GET", path)

        if use_json:
            output(data, use_json=True)
        else:
            campaigns = data.get("data", [])
            print(f"Draft Campaigns for {ctx.obj['label']}:")
            if not campaigns:
                print("  (none)")
                return
            for item in campaigns:
                attrs = item.get("attributes", {})
                created = (attrs.get("created_at") or "").split("T")[0]
                print(f"  [{item['id']}] \"{attrs.get('name', '?')}\" — created {created}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# get-campaign
# ---------------------------------------------------------------------------


@main.command("get-campaign")
@click.argument("campaign_id")
@click.pass_context
def get_campaign(ctx, campaign_id):
    """Show details for a specific campaign."""
    use_json = ctx.obj["json"]
    try:
        fields = (
            "fields[campaign]=name,status,send_time,created_at,audiences,send_strategy"
            "&include=campaign-messages&fields[campaign-message]=definition"
        )
        path = f"/api/campaigns/{campaign_id}/?{fields}"
        data = ctx.obj["call"]("GET", path)

        if use_json:
            output(data, use_json=True)
        else:
            item = data.get("data", {})
            attrs = item.get("attributes", {})
            audiences = attrs.get("audiences") or {}
            included = audiences.get("included") or []
            excluded = audiences.get("excluded") or []
            send_strategy = attrs.get("send_strategy") or {}
            # Subject + preview text live on the related campaign-message,
            # pulled in via include=campaign-messages. Surfacing them here lets
            # the scheduling workflow catch clone-leftover subject lines (an
            # email whose SL belongs to a different, cloned-from campaign).
            subject = preview = None
            for msg in data.get("included") or []:
                content = (
                    ((msg.get("attributes") or {}).get("definition") or {}).get("content") or {}
                )
                if content.get("subject") is not None or content.get("preview_text") is not None:
                    subject = content.get("subject")
                    preview = content.get("preview_text")
                    break
            print(f"Campaign: {attrs.get('name', '?')}")
            print(f"  ID: {item.get('id', '?')}")
            print(f"  Status: {attrs.get('status', '?')}")
            print(f"  Subject: {subject or '(none)'}")
            print(f"  Preview: {preview or '(none)'}")
            print(f"  Send Time: {attrs.get('send_time') or 'not scheduled'}")
            print(f"  Created: {(attrs.get('created_at') or '').split('T')[0]}")
            print("  Audiences:")
            print(f"    Include: {', '.join(included) or '(none)'}")
            print(f"    Exclude: {', '.join(excluded) or '(none)'}")
            print(f"  Send Strategy: {send_strategy.get('method', 'N/A')}")
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
        filter_str = f"equals(messages.channel,'email'),contains(name,'{query}')"
        encoded = urllib.parse.quote(filter_str)
        path = f"/api/campaigns/?filter={encoded}&fields[campaign]=name,status,send_time,created_at"
        data = ctx.obj["call"]("GET", path)

        if use_json:
            output(data, use_json=True)
        else:
            campaigns = data.get("data", [])
            print(f"Campaigns matching '{query}' for {ctx.obj['label']} ({len(campaigns)} results):")
            if not campaigns:
                print("  (none)")
                return
            for item in campaigns:
                attrs = item.get("attributes", {})
                status = attrs.get("status", "?")
                send_time = attrs.get("send_time")
                if send_time:
                    date_part = f"(send: {send_time.split('T')[0]})"
                else:
                    created = (attrs.get("created_at") or "").split("T")[0]
                    date_part = f"(created: {created})"
                print(f"  [{item['id']}] \"{attrs.get('name', '?')}\" — {status} {date_part}")
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
    try:
        attributes: dict = {}

        # Handle send time
        if date_val and time_val:
            attributes["send_strategy"] = {
                "method": "static",
                "datetime": _parse_send_time(date_val, time_val),
                "options": {
                    "is_local": False,
                },
            }
        elif date_val and not time_val:
            raise click.UsageError("--date requires --time as well.")
        elif time_val and not date_val:
            raise click.UsageError("--time requires --date as well.")

        # Handle audiences
        if include_ids is not None or exclude_ids is not None:
            included = (
                [i.strip() for i in include_ids.split(",") if i.strip()]
                if include_ids
                else []
            )
            excluded = (
                [i.strip() for i in exclude_ids.split(",") if i.strip()]
                if exclude_ids
                else []
            )
            attributes["audiences"] = {"included": included, "excluded": excluded}

        if not attributes:
            raise click.UsageError(
                "Provide at least one of: --date/--time, --include, --exclude"
            )

        payload = {
            "data": {
                "type": "campaign",
                "id": campaign_id,
                "attributes": attributes,
            }
        }

        data = ctx.obj["call"]("PATCH", f"/api/campaigns/{campaign_id}/", body=payload)

        if use_json:
            output(data, use_json=True)
        else:
            item = data.get("data", {})
            attrs = item.get("attributes", {})
            name = attrs.get("name")
            if name:
                print(f"Updated campaign: {name}")
                print(f"  Send Time: {attrs.get('send_time') or 'not set'}")
            else:
                print("Campaign updated.")
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
        payload = {
            "data": {
                "type": "campaign-send-job",
                "id": campaign_id,
            }
        }
        data = ctx.obj["call"]("POST", "/api/campaign-send-jobs/", body=payload)

        if use_json:
            output(data, use_json=True)
        else:
            errors = data.get("errors")
            if errors:
                raise click.ClickException(
                    f"Error scheduling campaign: {errors[0].get('detail', str(errors[0]))}"
                )
            print(f"Campaign {campaign_id} scheduled for sending.")
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

    # Fetch campaign details (name, send_time, subject lines) via include
    campaign_info = {}
    for cid in campaign_ids:
        try:
            resp = ctx.obj["call"](
                "GET",
                f"/api/campaigns/{cid}/"
                "?fields[campaign]=name,send_time,scheduled_at"
                "&include=campaign-messages"
                "&fields[campaign-message]=definition",
            )
            attrs = resp.get("data", {}).get("attributes", {})
            send_time = attrs.get("send_time") or attrs.get("scheduled_at") or ""
            send_date = send_time.split("T")[0] if send_time else ""
            name = attrs.get("name", "")

            # Get subject from first included message
            subject = ""
            preview = ""
            for inc in resp.get("included", []):
                if inc.get("type") == "campaign-message":
                    content = inc.get("attributes", {}).get("definition", {}).get("content", {})
                    subject = content.get("subject", "")
                    preview = content.get("preview_text", "")
                    break

            campaign_info[cid] = {
                "name": name,
                "send_date": send_date,
                "subject": subject,
                "preview": preview,
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
        msgs = ctx.obj["call"]("GET", f"/api/campaigns/{campaign_id}/campaign-messages/")
        results = []
        for m in msgs.get("data", []):
            attrs = m.get("attributes", {})
            content = (attrs.get("definition") or {}).get("content", {}) or {}
            tpl = ((m.get("relationships", {}) or {}).get("template", {}) or {}).get("data")
            html = text = None
            if tpl:
                tdata = ctx.obj["call"]("GET", f"/api/templates/{tpl['id']}/")
                tattrs = tdata.get("data", {}).get("attributes", {})
                html = tattrs.get("html")
                text = tattrs.get("text")
            results.append({
                "message_id": m.get("id"),
                "channel": attrs.get("channel"),
                "subject": content.get("subject"),
                "preview": content.get("preview_text"),
                "template_id": tpl.get("id") if tpl else None,
                "html": html, "text": text, "body": content.get("body"),
            })

        if use_json:
            output({"campaign_id": campaign_id, "messages": results}, use_json=True)
            return
        if not results:
            print("(no messages on this campaign)")
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
# list-campaigns
# ---------------------------------------------------------------------------


@main.command("list-campaigns")
@click.option("--status", default=None, help="Filter by status (e.g. Sent, Draft, Scheduled)")
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
    try:
        filters = [f"equals(messages.channel,'{channel}')"]
        if status:
            filters.append(f"equals(status,'{status}')")
        if days is not None:
            since_dt = datetime.now(ZoneInfo("UTC")) - timedelta(days=days)
            filters.append(f"greater-than(created_at,{since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')})")
        if since:
            filters.append(f"greater-than(created_at,{since}T00:00:00Z)")
        if until:
            filters.append(f"less-than(created_at,{until}T23:59:59Z)")
        filter_str = "and(" + ",".join(filters) + ")" if len(filters) > 1 else filters[0]
        encoded = urllib.parse.quote(filter_str)
        path = (f"/api/campaigns/?filter={encoded}&sort=-created_at"
                f"&fields[campaign]=name,status,send_time,created_at")

        campaigns: list = []
        while path and len(campaigns) < limit:
            data = ctx.obj["call"]("GET", path)
            campaigns.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None
        campaigns = campaigns[:limit]

        if use_json:
            output({"data": campaigns}, use_json=True)
        else:
            scope = f"channel={channel}" + (f", status={status}" if status else "")
            print(f"Campaigns for {ctx.obj['label']} ({scope}) — {len(campaigns)} shown:")
            for c in campaigns:
                a = c.get("attributes", {})
                st = a.get("send_time")
                when = (f"send {st.split('T')[0]}" if st
                        else f"created {(a.get('created_at') or '').split('T')[0]}")
                print(f"  [{c['id']}] {a.get('status', '?'):10} {a.get('name', '?')}  ({when})")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

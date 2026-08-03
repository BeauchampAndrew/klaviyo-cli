"""Flow commands: list, get, create, performance, and full step-by-step detail."""

import json as json_module
from datetime import datetime

import click

from .._util import _norm_name, output
from ..cli import main
from ..transport import APIError, AuthError, KLAVIYO_BASE

# ---------------------------------------------------------------------------
# flows
# ---------------------------------------------------------------------------


_FLOW_SORT = {"updated": "-updated", "created": "-created", "name": "name"}


@main.command("flows")
@click.option("--sort", "sort_key", type=click.Choice(["updated", "created", "name"]),
              default=None, help="Sort by updated/created (newest first) or name (A-Z)")
@click.option("--search", "search_term", default=None,
              help="Filter by name (case-insensitive substring)")
@click.pass_context
def flows(ctx, sort_key, search_term):
    """List all flows for a client, with optional sort and name search."""
    use_json = ctx.obj["json"]
    try:
        items: list = []
        path = "/api/flows/?fields[flow]=name,status,created,updated&page[size]=50"
        if sort_key:
            path += f"&sort={_FLOW_SORT[sort_key]}"
        while path:
            data = ctx.obj["call"]("GET", path)
            items.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        matches = items
        if search_term:
            term = search_term.lower()
            matches = [i for i in items
                       if term in (i.get("attributes", {}).get("name", "").lower())]

        if use_json:
            output({"data": matches}, use_json=True)
        else:
            if search_term:
                print(f"Flows matching '{search_term}' for {ctx.obj['label']} "
                      f"({len(matches)} of {len(items)}):")
            else:
                print(f"Flows for {ctx.obj['label']} ({len(matches)} total):")
            if not matches:
                print("  (none)")
                return
            for item in matches:
                attrs = item.get("attributes", {})
                updated = (attrs.get("updated") or "").split("T")[0] or "?"
                print(
                    f"  [{item['id']}] {attrs.get('name', '?')} — "
                    f"{attrs.get('status', '?')} (updated: {updated})"
                )
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# flow-performance
# ---------------------------------------------------------------------------


@main.command("flow-performance")
@click.option("--days", default=90, help="Days to look back")
@click.pass_context
def flow_performance(ctx, days):
    """Show flow revenue and engagement metrics."""
    use_json = ctx.obj["json"]
    try:
        from datetime import timedelta, timezone

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

        # Find Placed Order metric (try Shopify first, then Magento)
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

        # Get flow names (paginate; no status filter so manual/draft flows in
        # the report still resolve to a name instead of a raw ID)
        flow_names = {}
        path = "/api/flows/?fields[flow]=name&page[size]=50"
        while path:
            flows_resp = ctx.obj["call"]("GET", path)
            for f in flows_resp.get("data", []):
                flow_names[f["id"]] = f["attributes"]["name"]
            next_link = flows_resp.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        # Pull flow values report
        body = {
            "data": {
                "type": "flow-values-report",
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

        data = ctx.obj["call"]("POST", "/api/flow-values-reports/", body=body)
        results = data.get("data", {}).get("attributes", {}).get("results", [])

        # Aggregate by flow (multiple messages per flow)
        flow_totals: dict = {}
        for r in results:
            fid = r["groupings"]["flow_id"]
            stats = r["statistics"]
            if fid not in flow_totals:
                flow_totals[fid] = {"revenue": 0, "delivered": 0, "opens": 0, "clicks": 0, "unsubs": 0, "spam": 0}
            flow_totals[fid]["revenue"] += stats.get("conversion_value", 0)
            flow_totals[fid]["delivered"] += int(stats.get("delivered", 0))
            flow_totals[fid]["opens"] += int(stats.get("opens_unique", 0))
            flow_totals[fid]["clicks"] += int(stats.get("clicks_unique", 0))
            flow_totals[fid]["unsubs"] += int(stats.get("unsubscribes", 0))
            flow_totals[fid]["spam"] += int(stats.get("spam_complaints", 0))

        if use_json:
            output({"flows": flow_totals, "flow_names": flow_names}, use_json=True)
        else:
            total_rev = 0
            print(f"Flow Performance for {ctx.obj['label']} (last {days} days):\n")
            print(f"  {'Flow':<50} {'Revenue':>10} {'Sent':>8} {'Open%':>7} {'Click%':>7} {'Unsubs':>7}")
            print(f"  {'-'*93}")
            for fid, t in sorted(flow_totals.items(), key=lambda x: x[1]["revenue"], reverse=True):
                name = flow_names.get(fid, fid)[:48]
                orate = (t["opens"] / t["delivered"] * 100) if t["delivered"] > 0 else 0
                crate = (t["clicks"] / t["delivered"] * 100) if t["delivered"] > 0 else 0
                total_rev += t["revenue"]
                print(f"  {name:<50} ${t['revenue']:>9,.2f} {t['delivered']:>8,} {orate:>6.1f}% {crate:>6.1f}% {t['unsubs']:>7,}")
            print(f"\n  {'TOTAL':<50} ${total_rev:>9,.2f}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# flow-detail
# ---------------------------------------------------------------------------


@main.command("flow-detail")
@click.argument("flow_id")
@click.pass_context
def flow_detail(ctx, flow_id):
    """Show full flow structure: trigger, filters, emails with subjects, delays, splits."""
    use_json = ctx.obj["json"]
    try:
        import re as _re
        import time as _time

        # Get flow with definition (trigger + filters)
        flow_resp = ctx.obj["call"](
            "GET",
            f"/api/flows/{flow_id}/?additional-fields[flow]=definition",
        )
        flow_attrs = flow_resp.get("data", {}).get("attributes", {})
        flow_name = flow_attrs.get("name", "?")
        trigger_type = flow_attrs.get("trigger_type", "?")
        status = flow_attrs.get("status", "?")
        definition = flow_attrs.get("definition", {})

        # Get all flow actions
        actions = []
        path = f"/api/flows/{flow_id}/flow-actions/?fields[flow-action]=action_type,status,settings"
        while path:
            data = ctx.obj["call"]("GET", path)
            actions.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        # Get message details for each SEND_EMAIL action
        messages = {}
        for a in actions:
            if a["attributes"]["action_type"] == "SEND_EMAIL":
                _time.sleep(0.1)
                msg_resp = ctx.obj["call"]("GET", f"/api/flow-actions/{a['id']}/flow-messages/")
                for m in msg_resp.get("data", []):
                    content = m.get("attributes", {}).get("content", {})
                    messages[a["id"]] = {
                        "name": m["attributes"].get("name", ""),
                        "subject": content.get("subject", ""),
                        "preview": content.get("preview_text", ""),
                        "from_email": content.get("from_email", ""),
                        "from_label": content.get("from_label", ""),
                        "template_id": None,
                    }
                    # Get template ID for HTML
                    tmpl_resp = ctx.obj["call"]("GET", f"/api/flow-messages/{m['id']}/relationships/template/")
                    tmpl_id = tmpl_resp.get("data", {}).get("id")
                    messages[a["id"]]["template_id"] = tmpl_id

        if use_json:
            output({
                "flow": flow_attrs,
                "definition": definition,
                "actions": [a["attributes"] | {"id": a["id"]} for a in actions],
                "messages": messages,
            }, use_json=True)
        else:
            print(f"Flow: {flow_name}")
            print(f"Status: {status}")
            print(f"Trigger: {trigger_type}")

            if definition:
                triggers = definition.get("triggers", [])
                if triggers:
                    for t in triggers:
                        ttype = t.get("type", "?")
                        tid = t.get("id", "")
                        tfilter = t.get("trigger_filter")
                        print(f"  Trigger metric/list ID: {tid} (type: {ttype})")
                        if tfilter:
                            print(f"  Trigger filter: {json_module.dumps(tfilter)[:300]}")

                profile_filter = definition.get("profile_filter") or {}
                groups = profile_filter.get("condition_groups", [])
                if groups:
                    print(f"\nFlow Filters ({len(groups)} condition groups):")
                    for i, group in enumerate(groups):
                        for cond in group.get("conditions", []):
                            ctype = cond.get("type", "?")
                            if ctype == "profile-metric":
                                metric_id = cond.get("metric_id", "?")
                                measurement = cond.get("measurement", "?")
                                mfilter = cond.get("measurement_filter", {})
                                op = mfilter.get("operator", "?")
                                val = mfilter.get("value", "?")
                                tf = cond.get("timeframe_filter", {}).get("operator", "?")
                                print(f"  - Metric {metric_id}: {measurement} {op} {val} (timeframe: {tf})")
                            elif ctype == "profile-marketing-consent":
                                consent = cond.get("consent", {})
                                channel = consent.get("channel", "?")
                                can_receive = consent.get("can_receive_marketing", "?")
                                print(f"  - Marketing consent: {channel} = {can_receive}")
                            else:
                                print(f"  - {ctype}: {json_module.dumps(cond)[:200]}")

                reentry = definition.get("reentry_criteria", {})
                if reentry:
                    dur = reentry.get("duration", "?")
                    unit = reentry.get("unit", "?")
                    print(f"\nReentry: every {dur} {unit}(s)")

            print(f"\n{'='*80}")
            print(f"Flow Steps ({len(actions)} total):\n")

            step = 0
            for a in actions:
                attrs = a["attributes"]
                atype = attrs["action_type"]
                astatus = attrs["status"]

                if atype == "SEND_EMAIL":
                    step += 1
                    msg = messages.get(a["id"], {})
                    status_icon = "●" if astatus == "live" else "○"
                    print(f"  {status_icon} EMAIL {step}: {msg.get('name', '?')}")
                    print(f"    Subject: {msg.get('subject', '?')}")
                    print(f"    Preview: {msg.get('preview', '?')}")
                    print(f"    From: {msg.get('from_label', '')} <{msg.get('from_email', '')}>")
                    print(f"    Status: {astatus}")
                    print()

                elif atype == "SEND_SMS":
                    step += 1
                    print(f"  ● SMS {step}")
                    print(f"    Status: {astatus}")
                    print()

                elif atype == "TIME_DELAY":
                    settings = attrs.get("settings", {})
                    seconds = settings.get("delay_seconds", 0)
                    days_of_week = settings.get("days_of_week", [])
                    if seconds >= 86400:
                        delay_str = f"{seconds // 86400} day(s)"
                    elif seconds >= 3600:
                        delay_str = f"{seconds // 3600} hour(s)"
                    else:
                        delay_str = f"{seconds // 60} min"
                    dow = f" [{','.join(d[:3] for d in days_of_week)}]" if len(days_of_week) < 7 else ""
                    print(f"  ⏱ DELAY: {delay_str}{dow}")
                    print()

                elif atype == "BOOLEAN_BRANCH":
                    settings = attrs.get("settings", {})
                    print(f"  ⑂ SPLIT (conditional)")
                    if settings:
                        print(f"    {json_module.dumps(settings)[:300]}")
                    print()

                elif atype == "CONDITIONAL_SPLIT":
                    settings = attrs.get("settings", {})
                    print(f"  ⑂ CONDITIONAL SPLIT")
                    if settings:
                        print(f"    {json_module.dumps(settings)[:300]}")
                    print()

                elif atype == "TRIGGER_FILTER":
                    settings = attrs.get("settings", {})
                    print(f"  ⚡ TRIGGER FILTER")
                    if settings:
                        print(f"    {json_module.dumps(settings)[:300]}")
                    print()

                else:
                    print(f"  ? {atype} (status: {astatus})")
                    print()

    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# get-flow
# ---------------------------------------------------------------------------


@main.command("get-flow")
@click.argument("flow_id")
@click.option("--definition", "show_definition", is_flag=True,
              help="Include the flow definition (trigger, action chain, reentry)")
@click.pass_context
def get_flow(ctx, flow_id, show_definition):
    """Show a flow's basics in one call; --definition adds trigger, action chain, reentry.

    Cheap single-GET alternative to `flow-detail`, which walks every action
    and message with extra API calls to pull subjects, senders, and templates.
    Use this for a quick look; use `flow-detail` for the full step-by-step dump.
    """
    use_json = ctx.obj["json"]
    try:
        path = f"/api/flows/{flow_id}/"
        if show_definition:
            path += "?additional-fields[flow]=definition"
        data = ctx.obj["call"]("GET", path)
        if use_json:
            output(data, use_json=True)
            return
        attrs = data.get("data", {}).get("attributes", {})
        print(f"Flow: {attrs.get('name', '?')}")
        print(f"  ID: {flow_id}")
        print(f"  Status: {attrs.get('status', '?')}")
        print(f"  Trigger type: {attrs.get('trigger_type', '?')}")
        print(f"  Created: {(attrs.get('created') or '?').split('T')[0]}")
        print(f"  Updated: {(attrs.get('updated') or '?').split('T')[0]}")

        if show_definition:
            definition = attrs.get("definition") or {}
            for t in definition.get("triggers", []):
                print(f"  Trigger: {t.get('type', '?')} (id: {t.get('id', '?')})")

            actions = definition.get("actions", [])
            if actions:
                print(f"\nActions ({len(actions)} steps):")
                for i, a in enumerate(actions, 1):
                    atype = a.get("type", "?")
                    line = f"  {i}. {atype}"
                    if atype == "send-webhook":
                        a_data = a.get("data") or {}
                        url = a_data.get("url") or a_data.get("webhook_url")
                        if url:
                            line += f" -> {url}"
                    print(line)

            reentry = definition.get("reentry_criteria") or {}
            if reentry:
                dur = reentry.get("duration", "?")
                unit = reentry.get("unit", "?")
                print(f"\nReentry: every {dur} {unit}(s)")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# create-flow (dedup-guarded)
# ---------------------------------------------------------------------------


@main.command("create-flow")
@click.option("--body", "body_arg", required=True,
              help="Flow JSON (full data envelope, {name, definition}, or bare definition), or @file.json")
@click.option("--name", default=None,
              help="Flow name (required when --body is a bare definition)")
@click.option("--fix-ids", is_flag=True,
              help='Auto-rename action "id" keys to "temporary_id" (required by the create API)')
@click.option("--force", is_flag=True, help="Create even if a same-named flow exists")
@click.pass_context
def create_flow(ctx, body_arg, name, fix_ids, force):
    """Create a flow (in draft) from a definition, guarding against duplicates.

    Accepts a full {"data": ...} envelope, a {"name", "definition"} object,
    or a bare definition plus --name. Klaviyo's create API requires actions
    to carry "temporary_id" instead of "id" (entry_action_id and links.next
    reference those temporary_ids); pass --fix-ids to auto-rename.
    """
    use_json = ctx.obj["json"]
    try:
        if body_arg.startswith("@"):
            with open(body_arg[1:]) as f:
                parsed = json_module.load(f)
        else:
            parsed = json_module.loads(body_arg)

        # Accept: full {data:...} envelope, {name, definition} attributes, or bare definition.
        if isinstance(parsed, dict) and "data" in parsed:
            attrs = parsed["data"].get("attributes") or {}
            definition = attrs.get("definition") or {}
            flow_name = name or attrs.get("name")
        elif isinstance(parsed, dict) and "definition" in parsed:
            definition = parsed["definition"]
            flow_name = name or parsed.get("name")
        else:
            definition = parsed
            flow_name = name

        if not isinstance(definition, dict) or "actions" not in definition:
            raise click.ClickException(
                "Body must contain a flow definition with 'actions' (directly, "
                "under 'definition', or in a full data envelope)."
            )
        if not flow_name:
            raise click.UsageError("--name is required when the body doesn't include a name.")

        # The create API rejects actions keyed by "id" — it wants "temporary_id"
        # (and entry_action_id / links.next reference those temporary_ids).
        bad = [a for a in definition.get("actions", []) if isinstance(a, dict) and "id" in a]
        if bad:
            if fix_ids:
                for a in bad:
                    a["temporary_id"] = a.pop("id")
            else:
                raise click.ClickException(
                    f'{len(bad)} action(s) use "id", but the flow create API requires '
                    '"temporary_id" on submitted actions (entry_action_id and links.next '
                    "keep referencing the same values). Rename each action's \"id\" to "
                    '"temporary_id", or pass --fix-ids to auto-rename them.'
                )

        # Dedup guard — fetch existing flow names.
        existing: list = []
        path = "/api/flows/?fields[flow]=name&page[size]=50"
        while path:
            data = ctx.obj["call"]("GET", path)
            existing.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        target = _norm_name(flow_name)
        exact = [f for f in existing
                 if _norm_name(f.get("attributes", {}).get("name", "")) == target]
        if exact and not force:
            lines = "\n".join(f"  [{f['id']}] {f['attributes']['name']}" for f in exact)
            raise click.ClickException(
                f"A flow named '{flow_name}' already exists:\n{lines}\n"
                f"Reuse it, or pass --force to create a duplicate anyway."
            )

        payload = {"data": {"type": "flow",
                            "attributes": {"name": flow_name, "definition": definition}}}
        result = ctx.obj["call"]("POST", "/api/flows/", body=payload)
        new = result.get("data", {})
        if use_json:
            output(result, use_json=True)
        else:
            print(f"Created flow [{new.get('id', '?')}] "
                  f"{new.get('attributes', {}).get('name', flow_name)}")
            print("Flow created in draft. Review in Klaviyo UI before setting live.")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

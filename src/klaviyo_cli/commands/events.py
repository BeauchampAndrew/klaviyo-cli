"""Event commands: push custom events and list recent events for a metric."""

import json as json_module
import urllib.parse

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError

# ---------------------------------------------------------------------------
# push-event
# ---------------------------------------------------------------------------


def _parse_json_option(value: str | None, flag: str) -> dict:
    """Parse a JSON object option that accepts inline JSON or @file.json."""
    if value is None:
        return {}
    try:
        if value.startswith("@"):
            with open(value[1:]) as f:
                parsed = json_module.load(f)
        else:
            parsed = json_module.loads(value)
    except (ValueError, OSError) as e:
        raise click.UsageError(f"Invalid {flag}: {e}")
    if not isinstance(parsed, dict):
        raise click.UsageError(f"{flag} must be a JSON object.")
    return parsed


@main.command("push-event")
@click.option("--email", required=True, help="Profile email (profile is created if it doesn't exist)")
@click.option("--metric", "metric_name", required=True,
              help="Metric name (matched or created by name)")
@click.option("--time", "time_arg", default=None,
              help="Event timestamp, ISO 8601 (backdates the event)")
@click.option("--value", type=float, default=None,
              help="Numeric event value (e.g. order total)")
@click.option("--properties", default=None, help="Event properties JSON, or @file.json")
@click.option("--profile-attrs", default=None,
              help='Profile attributes JSON (e.g. {"first_name": "Ann"})')
@click.pass_context
def push_event(ctx, email, metric_name, time_arg, value, properties, profile_attrs):
    """Push a custom event to a profile by email.

    Creates the profile if it doesn't exist; the metric is matched or created
    by name. Use --time to backdate, --value for a numeric value, and
    --properties for event payload data.
    """
    use_json = ctx.obj["json"]
    props = _parse_json_option(properties, "--properties")
    prof_attrs = _parse_json_option(profile_attrs, "--profile-attrs")
    attributes: dict = {
        "properties": props,
        "metric": {"data": {"type": "metric", "attributes": {"name": metric_name}}},
        "profile": {"data": {"type": "profile",
                             "attributes": {"email": email, **prof_attrs}}},
    }
    if time_arg:
        attributes["time"] = time_arg
    if value is not None:
        attributes["value"] = value
    body = {"data": {"type": "event", "attributes": attributes}}
    try:
        result = ctx.obj["call"]("POST", "/api/events/", body=body)
        if use_json:
            # The Events API returns 202 with an empty body.
            output(result or {"status": "accepted"}, use_json=True)
        else:
            print("Event accepted (async; allow a minute to appear on the profile).")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


@main.command("events")
@click.option("--metric", "metric_id", required=True,
              help="Metric ID (from list-metrics)")
@click.option("--limit", default=10, help="Max events to show")
@click.option("--profile-id", default=None, help="Only events for this profile ID")
@click.option("--since", default=None, help="Only events on/after YYYY-MM-DD")
@click.option("--until", default=None, help="Only events before YYYY-MM-DD")
@click.option("--properties", "show_properties", is_flag=True,
              help="Print each event's event_properties JSON")
@click.pass_context
def events(ctx, metric_id, limit, profile_id, since, until, show_properties):
    """List recent events for a metric ID, newest first, with the profile attached.

    Useful for answering "who just did X" (e.g. find who was just
    unsuppressed). Metric IDs come from list-metrics. Use --since/--until to
    sample a window and --properties to inspect payloads (e.g. verify what a
    flow trigger split actually sees).
    """
    use_json = ctx.obj["json"]
    try:
        conditions = [f'equals(metric_id,"{metric_id}")']
        if profile_id:
            conditions.append(f'equals(profile_id,"{profile_id}")')
        # Datetime values are unquoted in Klaviyo filter syntax.
        if since:
            conditions.append(f"greater-or-equal(datetime,{since})")
        if until:
            conditions.append(f"less-than(datetime,{until})")
        filter_str = (conditions[0] if len(conditions) == 1
                      else f"and({','.join(conditions)})")
        encoded = urllib.parse.quote(filter_str)

        items: list = []
        included_all: dict = {}
        path = (f"/api/events/?filter={encoded}&sort=-datetime"
                f"&include=profile&page[size]={min(limit, 200)}")
        while path and len(items) < limit:
            data = ctx.obj["call"]("GET", path)
            items.extend(data.get("data", []))
            for i in data.get("included", []):
                if i.get("type") == "profile":
                    included_all[i["id"]] = i.get("attributes", {})
            next_link = data.get("links", {}).get("next")
            path = (next_link.replace("https://a.klaviyo.com", "")
                    if next_link else None)
        items = items[:limit]

        if use_json:
            output({"data": items, "included": list(included_all.values())},
                   use_json=True)
            return
        print(f"Events for metric {metric_id} ({len(items)} shown, newest first):")
        if not items:
            print("  (none)")
            return
        for ev in items:
            attrs = ev.get("attributes", {})
            pid = ((((ev.get("relationships") or {}).get("profile") or {})
                    .get("data") or {}).get("id"))
            p = included_all.get(pid, {})
            name = " ".join(
                x for x in [p.get("first_name"), p.get("last_name")] if x
            )
            who = p.get("email") or pid or "?"
            print(f"  {attrs.get('datetime', '?')}  {who}"
                  f"{('  (' + name + ')') if name else ''}")
            if show_properties:
                props = attrs.get("event_properties", {})
                print("    " + json_module.dumps(props, default=str))
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

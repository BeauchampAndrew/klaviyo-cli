"""delete: remove a segment, list, campaign, flow, or form after explicit confirmation.
segment-usage: the same "what uses it" scan on its own, read-only."""

import sys
from datetime import datetime, timedelta, timezone

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError, KLAVIYO_BASE, confirmed_delete
from .campaigns import _audience, _list_messages, _load, _status_label
from .forms import FORMS_REVISION, _form_status

# type -> (API collection, fields to show)
TYPES = {
    "segment": "segments",
    "list": "lists",
    "campaign": "campaigns",
    "flow": "flows",
    "form": "forms",
}

# Forms only exist at the newer API revision.
REVISIONS = {"form": FORMS_REVISION}


def _paginate(call, path):
    items = []
    while path:
        data = call("GET", path)
        items.extend(data.get("data", []))
        next_link = data.get("links", {}).get("next")
        path = next_link.replace(KLAVIYO_BASE, "") if next_link else None
    return items


def _flows_referencing(call, object_id):
    """Flows whose definition (trigger, filters, splits) mentions the ID."""
    hits = []
    for flow in _paginate(call, "/api/flows/?fields[flow]=name,status"):
        detail = call("GET", f"/api/flows/{flow['id']}/?additional-fields[flow]=definition")
        definition = detail.get("data", {}).get("attributes", {}).get("definition") or {}
        if object_id in str(definition):
            attrs = flow.get("attributes", {})
            hits.append(f"flow [{flow['id']}] {attrs.get('name', '?')} ({attrs.get('status', '?')})")
    return hits


def _broken(e: Exception) -> bool:
    return "broken state" in str(e).lower()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _messages_skipping_broken(call, status, skipped):
    """Campaign messages with this status. One message Klaviyo reports as being
    "in a broken state" fails every page it lands on, so on that error the
    created-date range is split in half until the bad message sits alone in a
    one-minute window; that window is noted in `skipped` and the rest is read."""
    base = f"equals(status,'{status}')"
    try:
        return _list_messages(call, [base])
    except APIError as e:
        if not _broken(e):
            raise
    rows = []
    windows = [(datetime(2012, 1, 1, tzinfo=timezone.utc),
                datetime.now(timezone.utc) + timedelta(days=1))]
    while windows:
        lo, hi = windows.pop()
        try:
            rows.extend(_list_messages(
                call, [base, f"greater-or-equal(created,{_iso(lo)})", f"less-than(created,{_iso(hi)})"]))
        except APIError as e:
            if not _broken(e):
                raise
            if hi - lo <= timedelta(minutes=1):
                skipped.append(f"{status} campaign created between {lo:%Y-%m-%d %H:%M} and "
                               f"{hi:%Y-%m-%d %H:%M} UTC (Klaviyo: broken state, unreadable)")
                continue
            mid = lo + (hi - lo) / 2
            windows += [(lo, mid), (mid, hi)]
    return rows


def _unsent_campaigns_referencing(call, object_id, skipped=None):
    """Draft/scheduled campaigns whose audiences include or exclude the ID.
    Campaigns that can't be read are added to `skipped` instead of aborting."""
    skipped = [] if skipped is None else skipped
    hits = []
    for status in ("draft", "scheduled"):
        for row in _messages_skipping_broken(call, status, skipped):
            attrs = row["attributes"]
            label = f"{attrs['channel'] or '?'} campaign [{row['id']}] {attrs['name'] or '?'} ({attrs['status']})"
            try:
                audience = _audience(call, {"id": row["id"], "audience_id": row["audience_id"]})
            except APIError as e:
                skipped.append(f"{label}: audience unreadable ({e})")
                continue
            if object_id in audience["included"] + audience["excluded"]:
                hits.append(label)
    return hits


def _usage(call, object_id):
    """(uses, skipped) across all flows and unsent campaigns."""
    skipped: list = []
    uses = _flows_referencing(call, object_id) + _unsent_campaigns_referencing(call, object_id, skipped)
    return uses, skipped


def _echo_usage(uses, skipped, deleting=True):
    if uses:
        click.echo(f"  ⚠ Still in use by {len(uses)} flow(s)/campaign(s):", err=True)
        for u in uses:
            click.echo(f"      {u}", err=True)
        if deleting:
            click.echo("    A flow triggered by it stops getting new entries.", err=True)
    else:
        click.echo("  Not used by any flow or draft/scheduled campaign.", err=True)
    if skipped:
        click.echo(f"  ⚠ Could not check {len(skipped)} campaign(s); check these by hand:", err=True)
        for s in skipped:
            click.echo(f"      {s}", err=True)


def _campaign_target(call, campaign_id):
    """(path, name, attrs) for deleting a campaign by message or container ID.
    A container holding just this message goes whole; otherwise only the message."""
    view = _load(call, campaign_id)
    siblings = call("GET", f"/api/campaigns/{view['campaign_id']}/campaign-messages/").get("data", [])
    if len(siblings) <= 1:
        path = f"/api/campaigns/{view['campaign_id']}/"
    else:
        path = f"/api/campaign-messages/{view['id']}/"
    return path, view["name"] or "?", {"status": _status_label(view["status"])}


@main.command("delete")
@click.argument("object_type", type=click.Choice(sorted(TYPES)))
@click.argument("object_id")
@click.option("--confirm", "confirm_id", default=None,
              help="The object's ID again, to confirm without a prompt (for scripts/agents)")
@click.pass_context
def delete(ctx, object_type, object_id, confirm_id):
    """Delete a segment, list, campaign, flow, or form. Permanent.

    Shows what will be deleted (and, for segments/lists, every flow and
    unsent campaign that uses it), then requires the ID typed back. In a
    non-interactive shell pass --confirm <ID> instead.
    """
    call = ctx.obj["call"]
    path = f"/api/{TYPES[object_type]}/{object_id}/"
    revision = REVISIONS.get(object_type)
    try:
        if object_type == "campaign":
            path, name, attrs = _campaign_target(call, object_id)
        else:
            extra = "?additional-fields[{0}]=profile_count".format(object_type) \
                if object_type in ("segment", "list") else ""
            data = call("GET", path + extra, revision=revision).get("data", {})
            attrs = data.get("attributes", {})
            name = attrs.get("name", "?")
            if object_type == "form":
                attrs = {**attrs, "status": _form_status(data)}

        click.echo(f"About to PERMANENTLY delete {object_type} [{object_id}] {name}", err=True)
        if attrs.get("status"):
            click.echo(f"  Status: {attrs['status']}", err=True)
        if attrs.get("profile_count") is not None:
            click.echo(f"  Profiles: {attrs['profile_count']:,} (profiles themselves are kept)", err=True)
        if object_type == "campaign" and attrs.get("status") == "Sent":
            click.echo("  ⚠ Sent campaign: its send history and reporting go with it.", err=True)
        if object_type == "flow" and attrs.get("status") == "live":
            click.echo("  ⚠ Live flow: anyone mid-flow stops receiving its messages.", err=True)
        if object_type == "form" and attrs["status"].startswith("live"):
            click.echo("  ⚠ Live form: it comes off the site, and its reporting goes with it.", err=True)

        if object_type in ("segment", "list"):
            _echo_usage(*_usage(call, object_id))

        if confirm_id is None:
            if not sys.stdin.isatty():
                raise click.ClickException(
                    f"Not deleted. Re-run with --confirm {object_id} to delete it.")
            confirm_id = click.prompt(f"Type {object_id} to delete it", err=True)
        if confirm_id != object_id:
            raise click.ClickException("Confirmation did not match the ID. Nothing deleted.")

        with confirmed_delete(path):
            call("DELETE", path, revision=revision)
        result = {"deleted": object_type, "id": object_id, "name": name}
        if ctx.obj["json"]:
            output(result, use_json=True)
        else:
            print(f"Deleted {object_type} [{object_id}] {name}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


@main.command("segment-usage")
@click.argument("object_id")
@click.pass_context
def segment_usage(ctx, object_id):
    """List every flow and draft/scheduled campaign that uses a segment or list.

    Checks flow triggers, filters and splits, and campaign include/exclude
    audiences. Read-only; run it before editing or deleting an audience.
    """
    call = ctx.obj["call"]
    try:
        uses, skipped = _usage(call, object_id)
        if ctx.obj["json"]:
            output({"id": object_id, "used_by": uses, "unchecked": skipped}, use_json=True)
            return
        click.echo(f"Usage of [{object_id}] in {ctx.obj['label']}:", err=True)
        _echo_usage(uses, skipped, deleting=False)
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

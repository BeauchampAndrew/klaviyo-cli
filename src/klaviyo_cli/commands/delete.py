"""delete: remove a segment, list, campaign, flow, or form after explicit confirmation."""

import sys

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


def _unsent_campaigns_referencing(call, object_id):
    hits = []
    for status in ("draft", "scheduled"):
        for row in _list_messages(call, [f"equals(status,'{status}')"]):
            audience = _audience(call, {"id": row["id"], "audience_id": row["audience_id"]})
            if object_id in audience["included"] + audience["excluded"]:
                attrs = row["attributes"]
                hits.append(f"{attrs['channel'] or '?'} campaign [{row['id']}] "
                            f"{attrs['name'] or '?'} ({attrs['status']})")
    return hits


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
            uses = _flows_referencing(call, object_id) + _unsent_campaigns_referencing(call, object_id)
            if uses:
                click.echo(f"  ⚠ Still in use by {len(uses)} flow(s)/campaign(s):", err=True)
                for u in uses:
                    click.echo(f"      {u}", err=True)
                click.echo("    A flow triggered by it stops getting new entries.", err=True)
            else:
                click.echo("  Not used by any flow or draft/scheduled campaign.", err=True)

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

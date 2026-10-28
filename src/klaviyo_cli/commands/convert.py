"""convert-template: turn an HTML (CODE) template into a drag-and-drop one.

Klaviyo's template-conversion-jobs endpoint: paid plans only (free accounts get
a 403), 1 job a minute, about 1-2 minutes per job. It creates a NEW template
and leaves the original alone. Tested on a real campaign email: text, links and
every image carried over (images re-hosted); the hidden preheader div is
dropped, which Klaviyo's own preview-text field replaces.
"""

import time

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError
from .campaigns import TEMPLATE_READ_REVISION, _load, _only_variation, _variation_details

POLL_SECONDS = 5
TIMEOUT_SECONDS = 300


def _convert(call, template_id: str) -> str:
    """Run a conversion job to completion; returns the new template's ID."""
    job = call("POST", "/api/template-conversion-jobs/", body={"data": {
        "type": "template-conversion-job",
        "relationships": {"source-template": {"data": {"type": "template", "id": template_id}}}}})
    job_id = (job.get("data") or {}).get("id")
    if not job_id:
        raise click.ClickException("Klaviyo didn't return a conversion job ID.")
    waited = 0
    while True:
        data = call("GET", f"/api/template-conversion-jobs/{job_id}/").get("data") or {}
        attrs = data.get("attributes") or {}
        if attrs.get("status") == "complete":
            new_id = ((((data.get("relationships") or {}).get("converted-template") or {})
                       .get("data")) or {}).get("id")
            if not new_id:
                raise click.ClickException(f"Conversion job {job_id} finished without a new template ID.")
            return new_id
        if attrs.get("status") == "failed":
            raise click.ClickException(f"Klaviyo couldn't convert template {template_id}: "
                                       f"{attrs.get('error') or 'no reason given'}")
        if waited >= TIMEOUT_SECONDS:
            raise click.ClickException(
                f"Conversion job {job_id} is still {attrs.get('status')} after {TIMEOUT_SECONDS // 60} "
                "minutes. Check Klaviyo's template library before re-running.")
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


@main.command("convert-template")
@click.argument("template_id", required=False)
@click.option("--campaign", "campaign_id", default=None,
              help="Convert a draft campaign's email and put the drag-and-drop version on the campaign")
@click.option("--name", default=None, help="Rename the new template")
@click.pass_context
def convert_template(ctx, template_id, campaign_id, name):
    """Convert an HTML template to drag-and-drop (paid Klaviyo plans only).

    \b
      klaviyo convert-template UpRZrn              # new drag-and-drop template, original untouched
      klaviyo convert-template --campaign <id>     # swap a draft campaign's email for the converted one

    Takes 1-2 minutes. Check the result in Klaviyo's editor before sending.
    """
    if bool(template_id) == bool(campaign_id):
        raise click.UsageError("Give a template ID or --campaign, not both.")
    try:
        call = ctx.obj["call"]
        variation = view = None
        if campaign_id:
            view = _load(call, campaign_id)
            if (view.get("status") or "").lower() != "draft":
                raise click.ClickException(
                    f"Campaign {view['id']} is {view.get('status')}; only a draft's email can be swapped.")
            variation = _only_variation(view)
            if variation.get("channel") != "email" or not variation.get("template_id"):
                raise click.ClickException(f"Campaign {view['id']} has no email template to convert.")
            template_id = variation["template_id"]

        source = ((call("GET", f"/api/templates/{template_id}/?fields[template]=name,editor_type",
                        revision=TEMPLATE_READ_REVISION).get("data") or {}).get("attributes") or {})
        if source.get("editor_type") != "CODE":
            raise click.ClickException(
                f"Template {template_id} is {source.get('editor_type')}, not HTML; nothing to convert.")
        if not ctx.obj["json"]:
            print(f"Converting {source.get('name') or template_id} (takes 1-2 minutes)...", flush=True)
        new_id = _convert(call, template_id)
        if name and not variation:  # a campaign gets its own copy, so naming the standalone is moot
            try:
                call("PATCH", f"/api/templates/{new_id}/", body={"data": {
                    "type": "template", "id": new_id, "attributes": {"name": name}}})
            except APIError as e:
                raise click.ClickException(f"Created template {new_id}, but renaming it failed: {e}")

        result = {"source_template_id": template_id, "template_id": new_id}
        if variation:
            # Assigning copies the template onto the campaign, so the standalone
            # converted template is deleted afterwards, as set-campaign-html does.
            try:
                call("POST", "/api/campaign-message-assign-template/", body={"data": {
                    "type": "campaign-message", "id": variation["id"],
                    "relationships": {"template": {"data": {"type": "template", "id": new_id}}}}})
                live = _variation_details(
                    call("GET", f"/api/campaign-variations/{variation['id']}/").get("data") or {})
            finally:
                call("DELETE", f"/api/templates/{new_id}/")
            live_id = live.get("template_id")
            editor = ((call("GET", f"/api/templates/{live_id}/?fields[template]=editor_type",
                            revision=TEMPLATE_READ_REVISION).get("data") or {}).get("attributes") or {}).get("editor_type")
            if live_id == template_id or editor != "SYSTEM_DRAGGABLE":
                raise click.ClickException(
                    f"Campaign {view['id']} didn't take the converted email (template {live_id}, {editor}).")
            result.update({"campaign_id": view["id"], "template_id": live_id})

        if ctx.obj["json"]:
            output(result, use_json=True)
        elif variation:
            print(f"Campaign {view['id']} now uses the drag-and-drop version (template {result['template_id']}).")
            print("  Check it in Klaviyo's editor before sending; the hidden preheader text isn't carried over.")
        else:
            print(f"Created drag-and-drop template {new_id}. The HTML original {template_id} is unchanged.")
    except (AuthError, APIError) as e:
        if "paid plan" in str(e):
            raise click.ClickException(f"{e} (this account is on Klaviyo's free plan).")
        raise click.ClickException(str(e))

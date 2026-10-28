"""test-send: email a test of a campaign, a flow email, or a template.

Klaviyo's preview-send endpoint mails a TEMPLATE, not a campaign, and the
test's subject is always "<template name> Preview" from "Klaviyo Preview
Mailer". So for a campaign or flow email this sends from a throwaway copy of
its template named after the real subject line, which puts the real subject
in the inbox, then deletes the copy (the send survives the delete: Klaviyo
renders the email when the job is queued).
"""

import urllib.parse

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError
from .campaigns import _load

MAX_RECIPIENTS = 5  # per request, Klaviyo's limit


def _message_steps(definition: dict) -> list[tuple[int, dict]]:
    """(number, action) for every email/SMS step, numbered the way flow-detail
    prints them: walk from the entry, YES branch before NO, then any
    unreachable steps."""
    by_id = {a.get("id") or a.get("temporary_id"): a for a in definition.get("actions", [])}
    seen: set = set()
    steps: list = []

    def visit(action):
        if action.get("type") in ("send-email", "send-sms"):
            steps.append((len(steps) + 1, action))

    def walk(aid):
        if aid is None or aid not in by_id or aid in seen:
            return
        seen.add(aid)
        action = by_id[aid]
        visit(action)
        links = action.get("links") or {}
        if "next_if_true" in links or "next_if_false" in links:
            walk(links.get("next_if_true"))
            walk(links.get("next_if_false"))
        else:
            nxt = links.get("next")
            for n in [nxt] if isinstance(nxt, str) else (nxt or []):
                walk(n)

    walk(definition.get("entry_action_id"))
    for aid, action in by_id.items():
        if aid not in seen:
            visit(action)
    return steps


def _flow_targets(call, flow_id: str, email_nums: str) -> list[dict]:
    resp = call("GET", f"/api/flows/{flow_id}/?additional-fields[flow]=definition")
    definition = ((resp.get("data") or {}).get("attributes") or {}).get("definition") or {}
    emails = [(n, a) for n, a in _message_steps(definition) if a.get("type") == "send-email"]
    if not emails:
        raise click.ClickException(f"Flow {flow_id} has no emails.")
    if email_nums.strip().lower() == "all":
        picked = emails
    else:
        try:
            wanted = {int(x) for x in email_nums.split(",") if x.strip()}
        except ValueError:
            raise click.UsageError("--email takes step numbers as flow-detail shows them "
                                   "(e.g. 1 or 1,3) or 'all'.")
        picked = [(n, a) for n, a in emails if n in wanted]
        missing = wanted - {n for n, _ in picked}
        if missing:
            have = ", ".join(str(n) for n, _ in emails)
            raise click.ClickException(
                f"No email numbered {', '.join(map(str, sorted(missing)))} in flow {flow_id} "
                f"(emails are {have}; numbers match flow-detail, which counts SMS steps too)."
            )
    targets = []
    for n, a in picked:
        msg = (a.get("data") or {}).get("message") or {}
        targets.append({"label": f"EMAIL {n}: {msg.get('name') or '?'}",
                        "template_id": msg.get("template_id"),
                        "subject": msg.get("subject_line"),
                        "preview": msg.get("preview_text"),
                        "from": msg.get("from_email")})
    return targets


def _campaign_targets(call, campaign_id: str) -> list[dict]:
    view = _load(call, campaign_id)
    emails = [v for v in view["variations"] if v.get("channel") == "email"]
    if not emails:
        raise click.ClickException(f"Campaign {view['id']} has no email content to test.")
    many = len(emails) > 1
    return [{"label": (f"{view['name']} (variation {i})" if many else view["name"]) or view["id"],
             "template_id": v.get("template_id"), "subject": v.get("subject"),
             "preview": v.get("preview_text"), "from": v.get("from_email")}
            for i, v in enumerate(emails, 1)]


def _profile_id(call, email: str) -> str:
    filt = urllib.parse.quote(f'equals(email,"{email}")')
    data = call("GET", f"/api/profiles/?filter={filt}&fields[profile]=email").get("data") or []
    if not data:
        raise click.ClickException(f"No profile with email {email} in this account.")
    return data[0]["id"]


def _send(call, template_id: str, recipients: list[str], profile_id: str | None) -> list[str]:
    job_ids = []
    for i in range(0, len(recipients), MAX_RECIPIENTS):
        body = {"data": {
            "type": "template-preview-send-job",
            "attributes": {"recipients": recipients[i:i + MAX_RECIPIENTS]},
            "relationships": {"template": {"data": {"type": "template", "id": template_id}}},
        }}
        if profile_id:
            body["data"]["relationships"]["profile"] = {"data": {"type": "profile", "id": profile_id}}
        job_ids.append((call("POST", "/api/template-preview-send-jobs/", body=body).get("data") or {}).get("id"))
    return job_ids


@main.command("test-send")
@click.argument("campaign_id", required=False)
@click.option("--to", "to", required=True, help="Comma-separated email addresses")
@click.option("--flow", "flow_id", default=None, help="Send a flow's email(s) instead of a campaign")
@click.option("--email", "email_nums", default=None,
              help="With --flow: email number(s) as flow-detail shows them (1 or 1,3) or 'all'")
@click.option("--template", "template_id", default=None, help="Send a template by ID")
@click.option("--as-profile", "as_email", default=None,
              help="Fill merge tags from this profile (email); default is a sample profile")
@click.pass_context
def send_test(ctx, campaign_id, to, flow_id, email_nums, template_id, as_email):
    """Email a test of a campaign, flow email(s), or template.

    \b
      klaviyo test-send <campaign_id> --to you@x.com,am@x.com
      klaviyo test-send --flow TCAqyz --email 1,3 --to you@x.com
      klaviyo test-send --template UpRZrn --to you@x.com

    The test arrives from "Klaviyo Preview Mailer" with the subject
    "<subject line> Preview". Preview text and sender aren't applied to tests,
    so they're printed here to check. Klaviyo allows 100 test sends a day per
    account.
    """
    use_json = ctx.obj["json"]
    picked = [x for x in (campaign_id, flow_id, template_id) if x]
    if len(picked) != 1:
        raise click.UsageError("Give exactly one of: a campaign ID, --flow, or --template.")
    if flow_id and not email_nums:
        raise click.UsageError("--flow needs --email (e.g. --email 1, --email 1,3, --email all).")
    if email_nums and not flow_id:
        raise click.UsageError("--email only applies with --flow.")
    recipients = [r.strip() for r in to.split(",") if r.strip()]
    if not recipients:
        raise click.UsageError("--to needs at least one email address.")
    try:
        call = ctx.obj["call"]
        if flow_id:
            targets = _flow_targets(call, flow_id, email_nums)
        elif template_id:
            targets = [{"label": f"template {template_id}", "template_id": template_id,
                        "subject": None, "preview": None, "from": None}]
        else:
            targets = _campaign_targets(call, campaign_id)
        profile_id = _profile_id(call, as_email) if as_email else None

        results = []
        for t in targets:
            if not t["template_id"]:
                raise click.ClickException(f"{t['label']} has no template to send.")
            send_from, temp_id = t["template_id"], None
            if t["subject"]:
                copy = call("POST", "/api/template-clone/", body={"data": {
                    "type": "template", "id": t["template_id"],
                    "attributes": {"name": t["subject"]}}})
                send_from = temp_id = (copy.get("data") or {}).get("id")
            try:
                jobs = _send(call, send_from, recipients, profile_id)
            finally:
                if temp_id:
                    call("DELETE", f"/api/templates/{temp_id}/")
            results.append({**t, "jobs": jobs})

        if use_json:
            output({"recipients": recipients, "sent": results}, use_json=True)
            return
        print(f"Test sent to {', '.join(recipients)}:")
        for r in results:
            print(f"  {r['label']}")
            if r["subject"]:
                print(f"    Subject: {r['subject']}  (arrives as \"{r['subject']} Preview\")")
            if r["preview"]:
                print(f"    Preview text: {r['preview']}  (not shown in tests)")
            if r["from"]:
                print(f"    From: {r['from']}  (tests come from Klaviyo Preview Mailer)")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

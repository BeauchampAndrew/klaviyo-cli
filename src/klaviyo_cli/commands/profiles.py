"""Profile commands: lookup, bulk suppression management, segment membership."""

import urllib.parse

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError, KLAVIYO_BASE

# ---------------------------------------------------------------------------
# get-profile
# ---------------------------------------------------------------------------


@main.command("get-profile")
@click.argument("identifier")
@click.option("--subscriptions", "show_subscriptions", is_flag=True,
              help="Include email marketing consent state and suppressions")
@click.pass_context
def get_profile(ctx, identifier, show_subscriptions):
    """Look up a profile by email or profile ID.

    An IDENTIFIER containing '@' is treated as an email address, anything
    else as a profile ID. With --subscriptions, also prints the email
    marketing consent state: whether the profile can receive email
    marketing, its consent status, and any suppressions (reason + timestamp).
    """
    use_json = ctx.obj["json"]
    try:
        extra = "additional-fields[profile]=subscriptions" if show_subscriptions else ""
        if "@" in identifier:
            filt = urllib.parse.quote(f'equals(email,"{identifier}")')
            path = f"/api/profiles/?filter={filt}"
            if extra:
                path += f"&{extra}"
            data = ctx.obj["call"]("GET", path)
            profiles = data.get("data", [])
            if not profiles:
                raise click.ClickException(f"No profile found for {identifier}")
            profile = profiles[0]
        else:
            path = f"/api/profiles/{identifier}/"
            if extra:
                path += f"?{extra}"
            data = ctx.obj["call"]("GET", path)
            profile = data.get("data", {})

        if use_json:
            output(data, use_json=True)
            return
        attrs = profile.get("attributes", {})
        name = " ".join(p for p in [attrs.get("first_name"), attrs.get("last_name")] if p)
        print(f"Profile: {attrs.get('email', '?')}")
        print(f"  ID: {profile.get('id', '?')}")
        print(f"  Name: {name or '?'}")
        print(f"  Created: {attrs.get('created') or '?'}")
        print(f"  Last event: {attrs.get('last_event_date') or '?'}")
        if show_subscriptions:
            marketing = ((attrs.get("subscriptions") or {}).get("email") or {}).get("marketing") or {}
            print("  Email marketing:")
            print(f"    Can receive: {marketing.get('can_receive_email_marketing', '?')}")
            print(f"    Consent: {marketing.get('consent', '?')}")
            suppressions = marketing.get("suppression") or []
            if suppressions:
                print("    Suppressions:")
                for s in suppressions:
                    print(f"      - {s.get('reason', '?')} at {s.get('timestamp', '?')}")
            else:
                print("    Suppressions: (none)")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# suppress / unsuppress (bulk jobs)
# ---------------------------------------------------------------------------


def _collect_emails(emails: str | None, file_path: str | None) -> list:
    """Resolve --emails / --file into a list of addresses."""
    if emails and file_path:
        raise click.UsageError("Pass --emails or --file, not both.")
    if emails:
        addresses = [e.strip() for e in emails.split(",") if e.strip()]
    elif file_path:
        with open(file_path) as f:
            addresses = [line.strip() for line in f if line.strip()]
    else:
        raise click.UsageError("Pass --emails a@x.com,b@y.com or --file emails.txt.")
    if not addresses:
        raise click.UsageError("No email addresses provided.")
    return addresses


def _suppression_body(job_type: str, addresses: list) -> dict:
    return {
        "data": {
            "type": job_type,
            "attributes": {
                "profiles": {
                    "data": [
                        {"type": "profile", "attributes": {"email": e}}
                        for e in addresses
                    ]
                }
            },
        }
    }


def _print_job_result(result: dict, verb: str, count: int) -> None:
    job = (result or {}).get("data") or {}
    job_id = job.get("id")
    status = (job.get("attributes") or {}).get("status")
    print(f"{verb} job submitted for {count} email(s).")
    if job_id:
        print(f"  Job [{job_id}] status: {status or '?'}")
    else:
        print("  Accepted (202). Check `suppression-jobs` for status.")


@main.command("suppress")
@click.option("--emails", default=None, help="Comma-separated email addresses")
@click.option("--file", "file_path", default=None, type=click.Path(exists=True),
              help="File with one email address per line")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def suppress(ctx, emails, file_path, yes):
    """Suppress profiles from email marketing (bulk, by email address).

    Suppressed profiles stop receiving all email sends until unsuppressed.
    Because this stops mail to real people, it asks for confirmation
    unless --yes is passed.
    """
    use_json = ctx.obj["json"]
    addresses = _collect_emails(emails, file_path)
    if not yes:
        click.confirm(
            f"Suppress {len(addresses)} profile(s) from email marketing?", abort=True
        )
    try:
        body = _suppression_body("profile-suppression-bulk-create-job", addresses)
        result = ctx.obj["call"](
            "POST", "/api/profile-suppression-bulk-create-jobs/", body=body
        )
        if use_json:
            output(result, use_json=True)
        else:
            _print_job_result(result, "Suppression", len(addresses))
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


@main.command("unsuppress")
@click.option("--emails", default=None, help="Comma-separated email addresses")
@click.option("--file", "file_path", default=None, type=click.Path(exists=True),
              help="File with one email address per line")
@click.pass_context
def unsuppress(ctx, emails, file_path):
    """Remove manual suppressions so profiles can receive email again (bulk).

    Only clears USER_SUPPRESSED suppressions. It never resubscribes anyone:
    unsubscribes, bounces, and spam complaints are untouched.
    """
    use_json = ctx.obj["json"]
    addresses = _collect_emails(emails, file_path)
    try:
        body = _suppression_body("profile-suppression-bulk-delete-job", addresses)
        result = ctx.obj["call"](
            "POST", "/api/profile-suppression-bulk-delete-jobs/", body=body
        )
        if use_json:
            output(result, use_json=True)
        else:
            _print_job_result(result, "Unsuppression", len(addresses))
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# unsubscribe (bulk consent revocation)
# ---------------------------------------------------------------------------

# Klaviyo caps subscription bulk jobs at 100 profiles per request.
_UNSUBSCRIBE_BATCH = 100


@main.command("unsubscribe")
@click.option("--emails", default=None, help="Comma-separated email addresses")
@click.option("--file", "file_path", default=None, type=click.Path(exists=True),
              help="File with one email address per line")
@click.option("--list", "list_id", default=None,
              help="Unsubscribe from this list only instead of all email marketing")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def unsubscribe(ctx, emails, file_path, list_id, yes):
    """Set profiles' email consent to UNSUBSCRIBED (bulk, by email address).

    Unlike `suppress`, this revokes consent rather than adding a removable
    suppression: an UNSUBSCRIBED profile cannot be resubscribed through the
    API — only the person can opt back in via a signup form. Works on
    NEVER_SUBSCRIBED profiles too. Any existing manual suppression is
    replaced by an UNSUBSCRIBE suppression (the activity log shows a
    "manually unsuppressed" event immediately followed by the unsubscribe;
    the profile is never mailable in between).

    Because this is irreversible from the account side, it asks for
    confirmation unless --yes is passed. Batches of 100 per API job.
    """
    use_json = ctx.obj["json"]
    addresses = _collect_emails(emails, file_path)
    if not yes:
        scope = f"list {list_id}" if list_id else "ALL email marketing"
        click.confirm(
            f"Unsubscribe {len(addresses)} profile(s) from {scope}? "
            "This cannot be undone via the API",
            abort=True,
        )
    try:
        results = []
        for start in range(0, len(addresses), _UNSUBSCRIBE_BATCH):
            batch = addresses[start:start + _UNSUBSCRIBE_BATCH]
            body = _suppression_body("profile-subscription-bulk-delete-job", batch)
            if list_id:
                body["data"]["relationships"] = {
                    "list": {"data": {"type": "list", "id": list_id}}
                }
            results.append(ctx.obj["call"](
                "POST", "/api/profile-subscription-bulk-delete-jobs/", body=body
            ))
        if use_json:
            output(results[0] if len(results) == 1 else {"jobs": results},
                   use_json=True)
        else:
            _print_job_result(results[0], "Unsubscribe", len(addresses))
            if len(results) > 1:
                print(f"  Submitted as {len(results)} jobs of up to "
                      f"{_UNSUBSCRIBE_BATCH}.")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# suppression-jobs
# ---------------------------------------------------------------------------


@main.command("suppression-jobs")
@click.option("--type", "job_type", type=click.Choice(["create", "delete", "both"]),
              default="both", help="Which job direction to list (default: both)")
@click.pass_context
def suppression_jobs(ctx, job_type):
    """List bulk suppression jobs (suppress + unsuppress) with status and counts."""
    use_json = ctx.obj["json"]
    try:
        jobs: list = []
        if job_type in ("create", "both"):
            data = ctx.obj["call"]("GET", "/api/profile-suppression-bulk-create-jobs/")
            jobs.extend(("suppress", j) for j in data.get("data", []))
        if job_type in ("delete", "both"):
            data = ctx.obj["call"]("GET", "/api/profile-suppression-bulk-delete-jobs/")
            jobs.extend(("unsuppress", j) for j in data.get("data", []))

        if use_json:
            output({"jobs": [{"direction": d, **j} for d, j in jobs]}, use_json=True)
            return
        print(f"Suppression jobs for {ctx.obj['label']} ({len(jobs)} total):")
        if not jobs:
            print("  (none)")
            return
        for direction, j in jobs:
            attrs = j.get("attributes", {})
            created = attrs.get("created") or attrs.get("created_at") or "?"
            print(f"  [{j.get('id', '?')}] {direction} — {attrs.get('status', '?')} "
                  f"(created: {created})")
            print(f"      total: {attrs.get('total_count', '?')}  "
                  f"completed: {attrs.get('completed_count', '?')}  "
                  f"skipped: {attrs.get('skipped_count', '?')}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# segment-members
# ---------------------------------------------------------------------------


@main.command("segment-members")
@click.argument("segment_id")
@click.option("--limit", default=20, help="Max profiles to show (paginates past 100)")
@click.pass_context
def segment_members(ctx, segment_id, limit):
    """List profiles in a segment: email, name, and when they joined."""
    use_json = ctx.obj["json"]
    try:
        page_size = min(limit, 100)
        profiles: list = []
        path = f"/api/segments/{segment_id}/profiles/?page[size]={page_size}"
        while path and len(profiles) < limit:
            data = ctx.obj["call"]("GET", path)
            profiles.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None
        profiles = profiles[:limit]

        if use_json:
            output({"data": profiles}, use_json=True)
            return
        print(f"Segment {segment_id} members for {ctx.obj['label']} "
              f"({len(profiles)} shown):")
        if not profiles:
            print("  (none)")
            return
        for p in profiles:
            attrs = p.get("attributes", {})
            name = " ".join(
                x for x in [attrs.get("first_name"), attrs.get("last_name")] if x
            )
            joined = attrs.get("joined_group_at") or "?"
            print(f"  [{p.get('id', '?')}] {attrs.get('email', '?')} "
                  f"{('(' + name + ') ') if name else ''}— joined {joined}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

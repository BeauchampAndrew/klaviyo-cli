"""list-remove: take people off a list. Their profiles are kept.

This only changes list membership. Profiles, their event history, consent and
every other list they're on stay exactly as they are. Deleting a profile is a
different, permanent action and is not done here.
"""

import sys
import urllib.parse

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError, confirmed_delete


def _profile(call, email):
    flt = urllib.parse.quote(f'equals(email,"{email}")')
    data = call("GET", f"/api/profiles/?filter={flt}&fields[profile]=email,first_name,last_name").get("data", [])
    return data[0] if data else None


def _on_list(call, profile_id, list_id):
    lists = call("GET", f"/api/profiles/{profile_id}/lists/?fields[list]=name").get("data", [])
    return any(lst.get("id") == list_id for lst in lists)


@main.command("list-remove")
@click.argument("list_id")
@click.option("--email", "emails", multiple=True, required=True,
              help="Email to take off the list. Repeat for several people.")
@click.option("--yes", is_flag=True,
              help="Skip the prompt (scripts/agents). The summary still prints first.")
@click.pass_context
def list_remove(ctx, list_id, emails, yes):
    """Take people off a list. Their profiles are kept.

    Prints the list, who comes off and who is skipped (no profile, or not on
    this list), and says what is NOT touched, then asks before removing. In a
    non-interactive shell pass --yes. Afterwards it re-checks each person and
    reports anyone still on the list.
    """
    call = ctx.obj["call"]
    try:
        name = call("GET", f"/api/lists/{list_id}/?fields[list]=name").get("data", {}) \
            .get("attributes", {}).get("name", "?")
        remove, skipped = [], []
        for raw in emails:
            email = raw.strip().lower()
            prof = _profile(call, email)
            if not prof:
                skipped.append((email, "no profile with this email"))
                continue
            if not _on_list(call, prof["id"], list_id):
                skipped.append((email, "not on this list"))
                continue
            attrs = prof.get("attributes", {})
            who = f"{attrs.get('first_name') or ''} {attrs.get('last_name') or ''}".strip()
            remove.append((prof["id"], email, who))

        click.echo(f"Remove {len(remove)} from list [{list_id}] {name}:", err=True)
        for _, email, who in remove:
            click.echo(f"  - {email}" + (f" ({who})" if who else ""), err=True)
        for email, why in skipped:
            click.echo(f"  skip {email}: {why}", err=True)
        click.echo("Only list membership changes. Their profiles, history, consent and other "
                   "lists are kept.", err=True)
        if not remove:
            raise click.ClickException("Nobody to remove.")

        if not yes:
            if not sys.stdin.isatty():
                raise click.ClickException("Not removed. Re-run with --yes to remove them.")
            if not click.confirm("Remove them from the list?", err=True):
                raise click.ClickException("Nothing removed.")

        path = f"/api/lists/{list_id}/relationships/profiles/"
        with confirmed_delete(path):
            call("DELETE", path, body={"data": [{"type": "profile", "id": pid} for pid, _, _ in remove]})

        still = [email for pid, email, _ in remove if _on_list(call, pid, list_id)]
        result = {
            "list": list_id,
            "name": name,
            "removed": [email for _, email, _ in remove if email not in still],
            "still_on_list": still,
            "skipped": [email for email, _ in skipped],
        }
        if ctx.obj["json"]:
            output(result, use_json=True)
        else:
            print(f"Removed {len(result['removed'])} from [{list_id}] {name}.")
            if still:
                print(f"Still on the list (check in Klaviyo): {', '.join(still)}")
        if still:
            raise SystemExit(1)
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

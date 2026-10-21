"""Sign-up form editing: form, form-replace, update-form, copy-form, publish-form, unpublish-form.

The Forms API (revision 2026-10-15) edits a form through its whole `definition`:
every PATCH replaces the stored definition, only draft versions may change,
live versions must come back exactly as received, and versions can't be added
or removed. Edits stay in draft until a publish job promotes them.
"""

import copy
import html
import json as json_module
import re
import sys

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError

FORMS_REVISION = "2026-10-15"

# Keys the API assigns; a definition posted as a new form must not carry them.
_SERVER_KEYS = ("id", "created_at", "updated_at")


def _get_form(call, form_id):
    data = call("GET", f"/api/forms/{form_id}/", revision=FORMS_REVISION)
    return data.get("data", {})


def _patch_definition(call, form_id, definition, name=None):
    attrs = {"definition": definition}
    if name:
        attrs["name"] = name
    body = {"data": {"type": "form", "id": form_id, "attributes": attrs}}
    return call("PATCH", f"/api/forms/{form_id}/", body=body, revision=FORMS_REVISION)


def _versions(form):
    return (form.get("attributes", {}).get("definition") or {}).get("versions") or []


def _form_status(form):
    """At this revision the form-level status reads "draft" whenever a draft
    version exists, even for a form that's on the site. Report live if any
    version is live."""
    versions = _versions(form)
    if any(v.get("status") == "live" for v in versions):
        return "live" + (", with draft" if any(v.get("status") == "draft" for v in versions) else "")
    return form.get("attributes", {}).get("status", "?")


def _explain(e):
    msg = str(e)
    if "at least one trigger" in msg:
        msg += (" (this form has no display rules; older forms often don't. Add one in the "
                "form editor, then retry.)")
    return msg


def _plain(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _blocks(version):
    """Yield (step_index, block) for every block in a version."""
    for si, step in enumerate(version.get("steps") or []):
        for col in step.get("columns") or []:
            for row in col.get("rows") or []:
                for block in row.get("blocks") or []:
                    yield si, block


def _block_text(block):
    props = block.get("properties") or {}
    parts = [_plain(props.get(k)) for k in ("content", "label", "placeholder") if props.get(k)]
    return " | ".join(p for p in parts if p)


def _replace_strings(node, old, new, path, hits):
    """Replace `old` in every string under `node` except server-assigned keys.
    Returns the new node; records (path, before, after) per changed string."""
    if isinstance(node, dict):
        return {k: (v if k in _SERVER_KEYS else _replace_strings(v, old, new, f"{path}.{k}", hits))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_strings(v, old, new, f"{path}[{i}]", hits) for i, v in enumerate(node)]
    if isinstance(node, str) and old in node:
        after = node.replace(old, new)
        hits.append((path, node, after))
        return after
    return node


def _strip_server_keys(node):
    if isinstance(node, dict):
        return {k: _strip_server_keys(v) for k, v in node.items() if k not in _SERVER_KEYS}
    if isinstance(node, list):
        return [_strip_server_keys(v) for v in node]
    return node


def _require_confirm(form_id, confirm_id, verb):
    if confirm_id is None:
        if not sys.stdin.isatty():
            raise click.ClickException(f"Not {verb}. Re-run with --confirm {form_id}.")
        confirm_id = click.prompt(f"Type {form_id} to confirm", err=True)
    if confirm_id != form_id:
        raise click.ClickException(f"Confirmation did not match the ID. Nothing {verb}.")


# ---------------------------------------------------------------------------
# form
# ---------------------------------------------------------------------------


@main.command("form")
@click.argument("form_id")
@click.option("--out", "out_path", default=None,
              help="Save the full definition JSON here (edit it, then update-form --definition @file)")
@click.pass_context
def form(ctx, form_id, out_path):
    """Show a form's versions and the text of every block, step by step."""
    try:
        f = _get_form(ctx.obj["call"], form_id)
        attrs = f.get("attributes", {})
        if out_path:
            with open(out_path, "w") as fh:
                json_module.dump(attrs.get("definition"), fh, indent=2)
        if ctx.obj["json"]:
            output(f, use_json=True)
            return
        print(f"Form [{form_id}] {attrs.get('name', '?')} ({_form_status(f)})")
        for v in _versions(f):
            ab = " A/B TEST" if v.get("ab_test") else ""
            print(f"\n  Version {v.get('id')} {v.get('status')} {v.get('type')} \"{v.get('name') or ''}\"{ab}")
            triggers = ", ".join(t.get("type", "?") for t in v.get("triggers") or [])
            print(f"    Triggers: {triggers or '(none)'}")
            for si, block in _blocks(v):
                text = _block_text(block)
                action = (block.get("action") or {}).get("type")
                extra = f"  -> {action}" if action else ""
                print(f"    step {si + 1}  {block.get('type', '?'):<14} {text[:90]}{extra}")
        if not any(v.get("status") == "draft" for v in _versions(f)):
            print("\n  No draft version: the API can only edit drafts. Open the form in the "
                  "editor once to create one, or copy-form it.")
        if out_path:
            print(f"\nDefinition saved to {out_path}")
    except (AuthError, APIError) as e:
        raise click.ClickException(_explain(e))


# ---------------------------------------------------------------------------
# form-replace
# ---------------------------------------------------------------------------


@main.command("form-replace")
@click.argument("form_id")
@click.argument("old")
@click.argument("new")
@click.option("--version", "version_id", default=None, help="Only this draft version (default: every draft)")
@click.option("--dry-run", is_flag=True, help="Show what would change; don't save")
@click.pass_context
def form_replace(ctx, form_id, old, new, version_id, dry_run):
    """Find/replace text in a form's draft version(s): copy, labels, URLs, anything.

    Live versions are never touched. Saved as draft; run publish-form to take it live.
    """
    call = ctx.obj["call"]
    try:
        f = _get_form(call, form_id)
        definition = copy.deepcopy(f.get("attributes", {}).get("definition") or {})
        hits = []
        live_only = False
        for i, v in enumerate(definition.get("versions") or []):
            if version_id and str(v.get("id")) != str(version_id):
                continue
            if v.get("status") != "draft":
                live_only = live_only or old in json_module.dumps(v)
                continue
            definition["versions"][i] = _replace_strings(v, old, new, f"version {v.get('id')}", hits)
        if not hits:
            msg = f"'{old}' not found in any draft version."
            if live_only:
                msg += " It is only in the live version, which the API can't edit."
            raise click.ClickException(msg)

        for path, before, after in hits:
            click.echo(f"  {path}\n    - {_plain(before)[:150]}\n    + {_plain(after)[:150]}", err=True)
        if dry_run:
            print(f"Dry run: {len(hits)} change(s), nothing saved.")
            return

        _patch_definition(call, form_id, definition)
        check = json_module.dumps([v for v in _versions(_get_form(call, form_id)) if v.get("status") == "draft"])
        if new and new not in check:
            raise click.ClickException("Saved, but the new text didn't read back. Check the form in the editor.")
        result = {"form": form_id, "changes": len(hits), "status": "draft"}
        if ctx.obj["json"]:
            output(result, use_json=True)
        else:
            print(f"Updated {len(hits)} string(s) in form {form_id} (verified, still draft).")
    except (AuthError, APIError) as e:
        raise click.ClickException(_explain(e))


# ---------------------------------------------------------------------------
# update-form
# ---------------------------------------------------------------------------


@main.command("update-form")
@click.argument("form_id")
@click.option("--definition", "definition_arg", required=True,
              help="Full definition JSON, as saved by `form --out` (string or @file)")
@click.option("--name", default=None, help="Also rename the form")
@click.pass_context
def update_form(ctx, form_id, definition_arg, name):
    """Replace a form's definition from an edited `form --out` file.

    Live versions are restored from the server before saving, so only draft
    edits go through. Version IDs must match the form's current versions.
    """
    call = ctx.obj["call"]
    try:
        if definition_arg.startswith("@"):
            with open(definition_arg[1:]) as fh:
                definition = json_module.load(fh)
        else:
            definition = json_module.loads(definition_arg)

        current = {str(v.get("id")): v for v in _versions(_get_form(call, form_id))}
        incoming = definition.get("versions") or []
        if sorted(str(v.get("id")) for v in incoming) != sorted(current):
            raise click.ClickException(
                f"Version IDs in the file ({', '.join(str(v.get('id')) for v in incoming)}) don't match "
                f"the form's ({', '.join(current)}). Versions can't be added or removed via the API.")
        for i, v in enumerate(incoming):
            server = current[str(v.get("id"))]
            if server.get("status") != "draft":
                if v != server:
                    click.echo(f"  Version {v.get('id')} is {server.get('status')}: "
                               "your edits to it were dropped (only drafts can change).", err=True)
                incoming[i] = server

        _patch_definition(call, form_id, definition, name=name)
        if ctx.obj["json"]:
            output({"form": form_id, "status": "draft"}, use_json=True)
        else:
            print(f"Saved definition for form {form_id} (draft). Run publish-form to take it live.")
    except (AuthError, APIError, OSError, ValueError) as e:
        raise click.ClickException(_explain(e))


# ---------------------------------------------------------------------------
# copy-form
# ---------------------------------------------------------------------------


@main.command("copy-form")
@click.argument("form_id")
@click.option("--name", required=True, help="Name for the new form")
@click.option("--version", "version_id", default=None,
              help="Version to copy (default: the live version, else the first)")
@click.pass_context
def copy_form(ctx, form_id, name, version_id):
    """Create a new draft form from one version of an existing form."""
    call = ctx.obj["call"]
    try:
        versions = _versions(_get_form(call, form_id))
        if version_id:
            picked = [v for v in versions if str(v.get("id")) == str(version_id)]
            if not picked:
                raise click.ClickException(f"Version {version_id} not found on form {form_id}.")
        else:
            picked = [v for v in versions if v.get("status") == "live"] or versions[:1]
        if not picked:
            raise click.ClickException(f"Form {form_id} has no versions to copy.")
        source = picked[0]
        if source.get("ab_test"):
            raise click.ClickException("That version is part of an A/B test; copy it in the form editor.")

        version = _strip_server_keys(source)
        version["status"] = "draft"
        body = {"data": {"type": "form", "attributes": {
            "name": name, "status": "draft", "ab_test": False,
            "definition": {"versions": [version]},
        }}}
        created = call("POST", "/api/forms/", body=body, revision=FORMS_REVISION).get("data", {})
        new_versions = _versions(created)
        result = {"form": created.get("id"), "name": name, "copied_from": f"{form_id} v{source.get('id')}",
                  "versions": [v.get("id") for v in new_versions], "status": "draft"}
        if ctx.obj["json"]:
            output(result, use_json=True)
        else:
            print(f"Created draft form [{created.get('id')}] {name}")
            print(f"  Copied from form {form_id}, version {source.get('id')} ({source.get('type')})")
            print(f"  New draft version: {', '.join(str(i) for i in result['versions'])}")
    except (AuthError, APIError) as e:
        raise click.ClickException(_explain(e))


# ---------------------------------------------------------------------------
# publish-form / unpublish-form
# ---------------------------------------------------------------------------


def _run_job(ctx, form_id, confirm_id, job_type, verb):
    call = ctx.obj["call"]
    try:
        f = _get_form(call, form_id)
        attrs = f.get("attributes", {})
        click.echo(f"About to {verb} form [{form_id}] {attrs.get('name', '?')} "
                   f"(currently {_form_status(f)}). This changes what site visitors see.", err=True)
        _require_confirm(form_id, confirm_id, verb + "ed")
        body = {"data": {"type": job_type,
                         "relationships": {"form": {"data": {"type": "form", "id": form_id}}}}}
        call("POST", f"/api/{job_type}s/", body=body, revision=FORMS_REVISION)
        after = _get_form(call, form_id)
        statuses = [f"v{v.get('id')} {v.get('status')}" for v in _versions(after)]
        result = {"form": form_id, "status": _form_status(after), "versions": statuses}
        if ctx.obj["json"]:
            output(result, use_json=True)
        else:
            print(f"{verb.capitalize()}ed form {form_id}: now {result['status']} ({', '.join(statuses)})")
    except (AuthError, APIError) as e:
        raise click.ClickException(_explain(e))


@main.command("publish-form")
@click.argument("form_id")
@click.option("--confirm", "confirm_id", default=None, help="The form ID again, to confirm without a prompt")
@click.pass_context
def publish_form(ctx, form_id, confirm_id):
    """Publish a form's draft: it replaces the live version on the site."""
    _run_job(ctx, form_id, confirm_id, "form-publish-job", "publish")


@main.command("unpublish-form")
@click.argument("form_id")
@click.option("--confirm", "confirm_id", default=None, help="The form ID again, to confirm without a prompt")
@click.pass_context
def unpublish_form(ctx, form_id, confirm_id):
    """Take a live form off the site (safe to re-run)."""
    _run_job(ctx, form_id, confirm_id, "form-unpublish-job", "unpublish")

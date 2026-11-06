"""Flow step editing: add/change delays, add emails, remove steps.

Steps are picked by message number, the EMAIL/SMS numbering flow-detail
prints (YES branch before NO, SMS counted), so `--before 2` means "right
before flow-detail's message 2".

Klaviyo's rules (POST/PATCH/DELETE /api/flow-actions):
  - New emails are created in Draft (with a copy of the template), so adding
    one to a live flow sends nothing until it's switched live.
  - Delays are always live. Adding one to a live flow, or changing or removing
    one there, affects profiles already in the flow, so it needs --live-ok.
  - Conditional/trigger splits and A/B tests can't be inserted. Trigger and
    profile filters can't be edited through the API at all.
"""

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError, confirmed_delete
from .test_send import _message_steps

MESSAGE_FIELDS = ("name", "from_email", "from_label", "reply_to_email", "cc_email", "bcc_email",
                  "subject_line", "preview_text", "template_id", "smart_sending_enabled",
                  "transactional", "add_tracking_params", "custom_tracking_params", "additional_filters")


def _load_flow(call, flow_id: str) -> dict:
    data = call("GET", f"/api/flows/{flow_id}/?additional-fields[flow]=definition").get("data") or {}
    attrs = data.get("attributes") or {}
    return {"id": flow_id, "name": attrs.get("name"), "status": attrs.get("status"),
            "definition": attrs.get("definition") or {}}


def _message(flow: dict, num: int) -> dict:
    steps = _message_steps(flow["definition"])
    for n, action in steps:
        if n == num:
            return action
    have = ", ".join(str(n) for n, _ in steps) or "none"
    raise click.ClickException(
        f"No message {num} in flow {flow['id']} (messages are {have}, numbered as in flow-detail).")


def _label(action: dict, num: int | None = None) -> str:
    if action.get("type") == "time-delay":
        d = action.get("data") or {}
        return f"delay of {d.get('value')} {d.get('unit') or 'days'}"
    msg = (action.get("data") or {}).get("message") or {}
    kind = "SMS" if action.get("type") == "send-sms" else "EMAIL"
    return f"{kind}{f' {num}' if num else ''}: {msg.get('name') or msg.get('subject_line') or '?'}"


def _delay_before(flow: dict, num: int) -> dict:
    target = _message(flow, num)["id"]
    for a in flow["definition"].get("actions", []):
        nxt = (a.get("links") or {}).get("next")
        if a.get("type") == "time-delay" and target in (nxt if isinstance(nxt, list) else [nxt]):
            return a
    raise click.ClickException(f"There's no delay directly before message {num} in flow {flow['id']}.")


def _amount(days, hours, minutes) -> tuple[str, int]:
    given = [(u, v) for u, v in (("days", days), ("hours", hours), ("minutes", minutes)) if v is not None]
    if len(given) != 1:
        raise click.UsageError("Give exactly one of --days, --hours, --minutes.")
    unit, value = given[0]
    if value < 0:
        raise click.UsageError("A delay can't be negative.")
    return unit, value


def _active(flow: dict) -> bool:
    """Anything but draft (live, manual) can have profiles waiting in it."""
    return flow["status"] != "draft"


def _needs_live_ok(flow: dict, live_ok: bool, what: str):
    if _active(flow) and not live_ok:
        raise click.ClickException(
            f"Flow {flow['id']} is {flow['status']}, and {what} affects profiles already in it right "
            "away. Re-run with --live-ok to go ahead.")


def _insert(call, flow: dict, definition: dict, before: int | None, after: int | None,
            live: bool = False) -> dict:
    """Insert `definition` (with temporary_id 'new') right before or after a message."""
    if (before is None) == (after is None):
        raise click.UsageError("Give exactly one of --before N or --after N (message numbers from flow-detail).")
    attrs = {"definition": definition}
    if live:
        attrs["allow_live_action"] = True
    if before is not None:
        definition["links"] = {"next": _message(flow, before)["id"]}
    else:
        anchor = _message(flow, after)
        attrs["anchor-action"] = {"data": {"type": "flow-action", "id": anchor["id"], "attributes": {
            "definition": {"type": anchor["type"], "links": {"next": definition["temporary_id"]}}}}}
    resp = call("POST", "/api/flow-actions/", body={"data": {"type": "flow-action", "attributes": attrs}})
    return resp.get("data") or {}


def _where(before, after) -> str:
    return f"before message {before}" if before is not None else f"after message {after}"


def _reachable(definition: dict) -> set[str]:
    by_id = {str(a.get("id")): a for a in definition.get("actions", [])}
    seen: set = set()
    todo = [definition.get("entry_action_id")]
    while todo:
        aid = todo.pop()
        if aid is None or str(aid) in seen or str(aid) not in by_id:
            continue
        seen.add(str(aid))
        for v in (by_id[str(aid)].get("links") or {}).values():
            todo.extend(v if isinstance(v, list) else [v])
    return seen


def _check_inserted(call, flow_id: str, new: dict) -> None:
    """The new step must be reachable from the flow's entry (POST isn't idempotent,
    so this reads back rather than retrying)."""
    new_id = new.get("id")
    if not new_id:
        raise click.ClickException(
            f"Klaviyo's response had no step ID; check flow-detail for flow {flow_id} before retrying.")
    if str(new_id) not in _reachable(_load_flow(call, flow_id)["definition"]):
        raise click.ClickException(
            f"Klaviyo created step {new_id} but it isn't connected in flow {flow_id} yet; check flow-detail "
            "before retrying (a retry would add a duplicate).")


time_options = [
    click.option("--days", type=int, default=None),
    click.option("--hours", type=int, default=None),
    click.option("--minutes", type=int, default=None),
]


def _time_options(f):
    for opt in reversed(time_options):
        f = opt(f)
    return f


@main.command("flow-add-delay")
@click.argument("flow_id")
@click.option("--before", type=int, default=None, help="Insert right before message N")
@click.option("--after", type=int, default=None, help="Insert right after message N")
@_time_options
@click.option("--live-ok", is_flag=True, help="Allow it in a live flow (affects profiles already in it)")
@click.pass_context
def flow_add_delay(ctx, flow_id, before, after, days, hours, minutes, live_ok):
    """Insert a time delay before or after a message.

    \b
      klaviyo flow-add-delay RDNDw6 --before 2 --days 1
      klaviyo flow-add-delay RDNDw6 --after 3 --hours 4
    """
    unit, value = _amount(days, hours, minutes)
    try:
        call = ctx.obj["call"]
        flow = _load_flow(call, flow_id)
        _needs_live_ok(flow, live_ok, "a new delay")
        new = _insert(call, flow, {"temporary_id": "new", "type": "time-delay",
                                   "data": {"unit": unit, "value": value, "timezone": "profile"}},
                      before, after, live=_active(flow))
        _check_inserted(call, flow_id, new)
        if ctx.obj["json"]:
            output({"flow_id": flow_id, "action": new}, use_json=True)
            return
        print(f"Added a {value} {unit} delay {_where(before, after)} in {flow['name']} "
              f"(step {new.get('id')}).")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


@main.command("flow-set-delay")
@click.argument("flow_id")
@click.option("--before", type=int, required=True, help="The delay right before message N")
@_time_options
@click.option("--live-ok", is_flag=True, help="Allow it in a live flow (affects profiles already in it)")
@click.pass_context
def flow_set_delay(ctx, flow_id, before, days, hours, minutes, live_ok):
    """Change the delay right before a message.

    \b
      klaviyo flow-set-delay RDNDw6 --before 2 --days 3
    """
    unit, value = _amount(days, hours, minutes)
    try:
        call = ctx.obj["call"]
        flow = _load_flow(call, flow_id)
        delay = _delay_before(flow, before)
        _needs_live_ok(flow, live_ok, "changing a delay")
        old = _label(delay)
        definition = {k: v for k, v in delay.items() if k in ("id", "type", "links", "data")}
        definition["data"] = {**(delay.get("data") or {}), "unit": unit, "value": value}
        call("PATCH", f"/api/flow-actions/{delay['id']}/", body={"data": {
            "type": "flow-action", "id": str(delay["id"]), "attributes": {"definition": definition}}})
        after = _delay_before(_load_flow(call, flow_id), before).get("data") or {}
        if (after.get("unit"), after.get("value")) != (unit, value):
            raise click.ClickException(f"Klaviyo didn't keep the change (the delay is {after}).")
        if ctx.obj["json"]:
            output({"flow_id": flow_id, "action_id": delay["id"], "unit": unit, "value": value}, use_json=True)
            return
        print(f"Delay before message {before} in {flow['name']}: {old} -> {value} {unit}.")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


@main.command("flow-add-email")
@click.argument("flow_id")
@click.option("--before", type=int, default=None, help="Insert right before message N")
@click.option("--after", type=int, default=None, help="Insert right after message N")
@click.option("--copy", "copy_num", type=int, default=None,
              help="Start from message N's settings and template (sender, subject, filters...)")
@click.option("--template", "template_id", default=None, help="Template to use (Klaviyo copies it)")
@click.option("--subject", default=None)
@click.option("--preview", default=None, help="Preview text")
@click.option("--name", default=None, help="Email name shown in the flow")
@click.pass_context
def flow_add_email(ctx, flow_id, before, after, copy_num, template_id, subject, preview, name):
    """Insert an email (created in Draft) before or after a message.

    \b
      klaviyo flow-add-email RDNDw6 --after 3 --copy 3 --name "EM4" --subject "Last chance"
      klaviyo flow-add-email RDNDw6 --before 1 --template UpRZrn --subject "Welcome"

    The new email stays in Draft until it's switched live in Klaviyo, so it
    sends nothing yet. Klaviyo gives it its own copy of the template.
    """
    try:
        call = ctx.obj["call"]
        flow = _load_flow(call, flow_id)
        message = {}
        if copy_num is not None:
            source = _message(flow, copy_num)
            if source.get("type") != "send-email":
                raise click.ClickException(f"Message {copy_num} is SMS; --copy takes an email.")
            src = (source.get("data") or {}).get("message") or {}
            message = {k: src[k] for k in MESSAGE_FIELDS if src.get(k) is not None}
        for key, value in (("template_id", template_id), ("subject_line", subject),
                           ("preview_text", preview), ("name", name)):
            if value is not None:
                message[key] = value
        if not message.get("subject_line"):
            raise click.UsageError("Give --subject, or --copy N to start from another email.")
        new = _insert(call, flow, {"temporary_id": "new", "type": "send-email",
                                   "data": {"message": message, "status": "draft"}},
                      before, after)
        _check_inserted(call, flow_id, new)
        saved = (((new.get("attributes") or {}).get("definition") or {}).get("data") or {}).get("message") or {}
        if ctx.obj["json"]:
            output({"flow_id": flow_id, "action": new}, use_json=True)
            return
        print(f"Added a draft email {_where(before, after)} in {flow['name']} (step {new.get('id')}).")
        print(f"  Subject: {saved.get('subject_line') or message.get('subject_line')}")
        if saved.get("template_id"):
            print(f"  Template: {saved['template_id']} (edit with: edit-template {saved['template_id']})")
        print("  Status: draft (switch it live in Klaviyo when it's ready)")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


@main.command("flow-remove-step")
@click.argument("flow_id")
@click.option("--message", "message_num", type=int, default=None, help="Remove message N")
@click.option("--delay-before", type=int, default=None, help="Remove the delay right before message N")
@click.option("--live-ok", is_flag=True, help="Allow it in a live flow")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def flow_remove_step(ctx, flow_id, message_num, delay_before, live_ok, yes):
    """Remove a message or a delay from a flow. Klaviyo reconnects the steps around it.

    \b
      klaviyo flow-remove-step RDNDw6 --delay-before 2
      klaviyo flow-remove-step RDNDw6 --message 4 --yes

    A removed email is gone, along with its place in the flow's reporting.
    """
    if (message_num is None) == (delay_before is None):
        raise click.UsageError("Give exactly one of --message N or --delay-before N.")
    try:
        call = ctx.obj["call"]
        flow = _load_flow(call, flow_id)
        if message_num is not None:
            action, what = _message(flow, message_num), _label(_message(flow, message_num), message_num)
        else:
            action = _delay_before(flow, delay_before)
            what = f"the {_label(action)} before message {delay_before}"
        _needs_live_ok(flow, live_ok, "removing a step")
        if not yes and not click.confirm(f"Remove {what} from {flow['name']} ({flow['status']})?"):
            raise click.ClickException("Nothing removed.")
        path = f"/api/flow-actions/{action['id']}/"
        with confirmed_delete(path):
            call("DELETE", path)
        left = {str(a.get("id")) for a in _load_flow(call, flow_id)["definition"].get("actions", [])}
        if str(action["id"]) in left:
            raise click.ClickException(f"Klaviyo still shows step {action['id']} in the flow.")
        if ctx.obj["json"]:
            output({"flow_id": flow_id, "removed": action["id"]}, use_json=True)
            return
        print(f"Removed {what} from {flow['name']}.")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


EMAIL_EDITS = (("subject", "subject_line"), ("preview", "preview_text"),
               ("from_label", "from_label"), ("from_email", "from_email"))


def _read_text(value: str) -> str:
    if value.startswith("@@"):
        return value[1:]
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as fh:
            return fh.read().rstrip("\n")
    return value


def _apply_replace(text: str, pairs, where: str) -> str:
    for old, new in pairs:
        if old not in text:
            raise click.ClickException(f'"{old}" isn\'t in the {where}, nothing changed.')
        text = text.replace(old, new)
    return text


@main.command("flow-edit-message")
@click.argument("flow_id")
@click.option("--message", "message_num", type=int, required=True, help="Message N, numbered as in flow-detail")
@click.option("--body", default=None, help="SMS: the new text (or @file)")
@click.option("--replace", "pairs", nargs=2, multiple=True, metavar="OLD NEW",
              help="SMS: replace text in the body. Email: in the subject/preview (repeatable)")
@click.option("--subject", default=None, help="Email subject line")
@click.option("--preview", default=None, help="Email preview text")
@click.option("--from-label", default=None, help="Email sender name")
@click.option("--from-email", default=None, help="Email sender address")
@click.option("--dry-run", is_flag=True, help="Show the change without saving")
@click.pass_context
def flow_edit_message(ctx, flow_id, message_num, body, pairs, subject, preview, from_label,
                      from_email, dry_run):
    """Change a flow message's text: an SMS body, or an email's subject, preview or sender.

    \b
      klaviyo flow-edit-message RDNDw6 --message 2 --replace "expire on" "expire"
      klaviyo flow-edit-message RDNDw6 --message 2 --body @sms.txt --dry-run
      klaviyo flow-edit-message RDNDw6 --message 1 --subject "Last chance" --preview "Ends tonight"

    Works on live flows: the next send uses the new text. For an email's body
    copy, use edit-template --flow FLOW_ID --email N.
    """
    try:
        call = ctx.obj["call"]
        flow = _load_flow(call, flow_id)
        action = _message(flow, message_num)
        is_sms = action.get("type") == "send-sms"
        email_vals = {key: val for (_, key), val in zip(EMAIL_EDITS, (subject, preview, from_label, from_email))
                      if val is not None}
        if is_sms and email_vals:
            raise click.UsageError(f"Message {message_num} is SMS; use --body or --replace.")
        if not is_sms and body is not None:
            raise click.UsageError(f"Message {message_num} is an email; --body is for SMS "
                                   f"(email copy: edit-template --flow {flow_id} --email {message_num}).")
        if body is None and not pairs and not email_vals:
            raise click.UsageError("Nothing to change: give --body, --replace, or an email field.")

        data = action.get("data") or {}
        message = dict(data.get("message") or {})
        changes = {}
        if is_sms:
            old_body = message.get("body") or ""
            new_body = _read_text(body) if body is not None else old_body
            new_body = _apply_replace(new_body, pairs, "SMS body")
            if new_body != old_body:
                changes["body"] = (old_body, new_body)
                message["body"] = new_body
                hierarchy = []
                for h in message.get("message_hierarchy") or []:
                    h = dict(h)
                    if body is not None:
                        h["message"] = new_body
                    else:
                        text = h.get("message") or ""
                        for o, n in pairs:
                            text = text.replace(o, n)
                        h["message"] = text
                    hierarchy.append(h)
                if hierarchy:
                    message["message_hierarchy"] = hierarchy
        else:
            for key in ("subject_line", "preview_text"):
                if pairs and message.get(key):
                    replaced = message[key]
                    for old, new in pairs:
                        replaced = replaced.replace(old, new)
                    email_vals.setdefault(key, replaced)
            if pairs and not any(old in (message.get(k) or "") for old, _ in pairs for k in ("subject_line", "preview_text")):
                raise click.ClickException("None of the --replace text is in the subject or preview. "
                                           f"For body copy use edit-template --flow {flow_id} --email {message_num}.")
            for key, val in email_vals.items():
                if message.get(key) != val:
                    changes[key] = (message.get(key), val)
                    message[key] = val

        label = _label(action, message_num)
        if not changes:
            raise click.ClickException(f"{label} already says that; nothing changed.")
        if ctx.obj["json"] and dry_run:
            output({"flow_id": flow_id, "action_id": action["id"], "dry_run": True,
                    "changes": {k: {"before": a, "after": b} for k, (a, b) in changes.items()}}, use_json=True)
            return
        if not ctx.obj["json"]:
            print(f"{label} in {flow['name']} ({flow['status']}, message {data.get('status')}):")
            for key, (before, after) in changes.items():
                print(f"  {key}\n    before: {before}\n    after:  {after}")
        if dry_run:
            print("Dry run, nothing saved.")
            return

        definition = {k: v for k, v in action.items() if k in ("id", "type", "links", "data")}
        definition["data"] = {**data, "message": message}
        call("PATCH", f"/api/flow-actions/{action['id']}/", body={"data": {
            "type": "flow-action", "id": str(action["id"]), "attributes": {"definition": definition}}})
        saved = ((_message(_load_flow(call, flow_id), message_num).get("data") or {}).get("message") or {})
        wrong = [k for k, (_, after) in changes.items() if saved.get(k) != after]
        if wrong:
            raise click.ClickException(f"Klaviyo didn't keep {', '.join(wrong)}; check flow-detail {flow_id}.")
        if ctx.obj["json"]:
            output({"flow_id": flow_id, "action_id": action["id"],
                    "changes": {k: {"before": a, "after": b} for k, (a, b) in changes.items()}}, use_json=True)
            return
        print("Saved and verified." + (" Live: the next send uses it." if data.get("status") == "live" else ""))
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


STATUSES = ("live", "manual", "draft")


def _set_action_status(call, action: dict, status: str) -> None:
    definition = {k: v for k, v in action.items() if k in ("id", "type", "links", "data")}
    definition["data"] = {**(action.get("data") or {}), "status": status}
    call("PATCH", f"/api/flow-actions/{action['id']}/", body={"data": {
        "type": "flow-action", "id": str(action["id"]), "attributes": {"definition": definition}}})


@main.command("flow-set-status")
@click.argument("flow_id")
@click.argument("status", type=click.Choice(STATUSES))
@click.option("--message", "message_nums", type=int, multiple=True,
              help="Also set message N (repeatable, numbered as in flow-detail)")
@click.option("--all-messages", is_flag=True, help="Also set every email and SMS in the flow")
@click.option("--messages-only", is_flag=True, help="Change only the messages, leave the flow's status")
@click.option("--dry-run", is_flag=True, help="Show what would change without saving")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt when going live")
@click.pass_context
def flow_set_status(ctx, flow_id, status, message_nums, all_messages, messages_only, dry_run, yes):
    """Set a flow live, manual or draft, and optionally its messages too.

    \b
      klaviyo flow-set-status RDNDw6 live --all-messages      # every message, then the flow
      klaviyo flow-set-status RDNDw6 live --message 3 --messages-only
      klaviyo flow-set-status RDNDw6 draft                    # pause the whole flow
      klaviyo flow-set-status RDNDw6 manual --message 2 --messages-only

    A live flow only sends messages that are themselves live, so a new flow
    usually needs --all-messages. Going live asks first unless --yes.
    Turning a flow off stops profiles already waiting in it.
    """
    if messages_only and not (message_nums or all_messages):
        raise click.UsageError("--messages-only needs --message N or --all-messages.")
    if message_nums and all_messages:
        raise click.UsageError("Give --message N or --all-messages, not both.")
    try:
        call = ctx.obj["call"]
        flow = _load_flow(call, flow_id)
        steps = _message_steps(flow["definition"])
        nums = [n for n, _ in steps] if all_messages else list(message_nums)
        targets = [(n, _message(flow, n)) for n in nums]
        to_change = [(n, a) for n, a in targets if (a.get("data") or {}).get("status") != status]
        flow_change = not messages_only and flow["status"] != status

        plan = [f"  {_label(a, n)}: {(a.get('data') or {}).get('status')} -> {status}" for n, a in to_change]
        if flow_change:
            plan.append(f"  Flow: {flow['status']} -> {status}")
        if not plan:
            raise click.ClickException(f"{flow['name']} is already {status}"
                                       + (" with those messages." if targets else ".") + " Nothing changed.")
        if ctx.obj["json"] and dry_run:
            output({"flow_id": flow_id, "dry_run": True, "status": status, "flow": flow_change,
                    "messages": [a["id"] for _, a in to_change]}, use_json=True)
            return
        if not ctx.obj["json"]:
            print(f"{flow['name']} ({flow_id}):")
            print("\n".join(plan))
            if status == "live" and flow_change and not targets:
                drafts = [n for n, a in steps if (a.get("data") or {}).get("status") != "live"]
                if drafts:
                    print(f"  Note: messages {', '.join(map(str, drafts))} aren't live and won't send "
                          "(add --all-messages to switch them too).")
        if dry_run:
            print("Dry run, nothing saved.")
            return
        if status == "live" and not yes and not click.confirm("Go live? Real profiles will start getting these."):
            raise click.ClickException("Nothing changed.")

        def set_flow():
            call("PATCH", f"/api/flows/{flow_id}/", body={"data": {
                "type": "flow", "id": flow_id, "attributes": {"status": status}}})

        # Going live: messages first so the flow never runs with drafts. Pausing: flow first.
        if flow_change and status != "live":
            set_flow()
        for _, action in to_change:
            _set_action_status(call, action, status)
        if flow_change and status == "live":
            set_flow()

        after = _load_flow(call, flow_id)
        by_id = {str(a.get("id")): a for a in after["definition"].get("actions", [])}
        wrong = [_label(a, n) for n, a in to_change
                 if ((by_id.get(str(a["id"])) or {}).get("data") or {}).get("status") != status]
        if flow_change and after["status"] != status:
            wrong.append(f"the flow (still {after['status']})")
        if wrong:
            raise click.ClickException(f"Klaviyo didn't keep: {'; '.join(wrong)}. Check flow-actions {flow_id}.")
        if ctx.obj["json"]:
            output({"flow_id": flow_id, "status": status, "flow": after["status"],
                    "messages": [a["id"] for _, a in to_change]}, use_json=True)
            return
        print(f"Saved and verified. Flow is {after['status']}.")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

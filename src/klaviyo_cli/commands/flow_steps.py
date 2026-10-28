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

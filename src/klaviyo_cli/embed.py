"""Embedding API: lets a host package re-expose every klaviyo-cli command with
its own auth (e.g. an agency CLI with per-client credentials and a leading
ACCOUNT positional).

Kept stable on purpose: host packages (e.g. workspace-cli) import
``wrap_with_account`` and ``build_host_group`` directly.

How the click plumbing works, for anyone touching this file:

Every ported command is defined with ``@click.pass_context``, which click
implements as a thin wrapper::

    def new_func(*args, **kwargs):
        return f(click.get_current_context(), *args, **kwargs)

``get_current_context()`` reads whatever context click currently has pushed
on its context stack -- it does NOT care which Command object the callback
is attached to. Click pushes that context once, in
``Context.invoke()``/``Command.invoke()``, via a bare ``with ctx:`` around
the raw callback call; nothing in the call chain below that point pushes a
*new* context unless it explicitly asks click to.

That means ``wrap_with_account`` can build a brand new ``click.Command``
whose callback is a plain (non-``pass_context``) function: when click invokes
*that* command it pushes the wrapped command's own sub-context, our callback
mutates ``ctx.obj`` on that same context object, and then calls the
original ``pass_context``-wrapped callback directly (not through
``ctx.invoke``/``Command.invoke`` again). Since we never push a second
context, ``get_current_context()`` inside the original callback still
resolves to the exact context we just mutated -- so ``ctx.obj["call"]`` /
``ctx.obj["label"]`` / ``ctx.obj["json"]`` all show up correctly, with no
double-invocation and no missing ctx injection. Verified by the tests in
``tests/test_embed.py`` (including one that checks positional binding when
ACCOUNT is inserted ahead of a command's own required argument).
"""

from typing import Callable

import click

from .cli import main


def wrap_with_account(
    cmd: click.Command, resolver: Callable[[str], object], arg_name: str = "account"
) -> click.Command:
    """Return a copy of ``cmd`` with a required leading ACCOUNT positional.

    At invoke time, the account value is popped off before the original
    callback ever sees its kwargs, and ``resolver(account)`` is called
    lazily -- only when the command body actually makes an API call -- so
    ``--help`` never touches auth.
    """
    orig_callback = cmd.callback
    account_param = click.Argument([arg_name])

    def callback(*args, **kwargs):
        # click normalizes the param decl into a Python-identifier dest name
        # (dashes -> underscores) when building kwargs, so we must pop by
        # account_param.name, not the raw arg_name -- otherwise any
        # dashed arg_name (e.g. "client-id") KeyErrors on every invocation.
        account = kwargs.pop(account_param.name)
        ctx = click.get_current_context()
        transport = None

        def call(method, path, body=None, revision=None):
            nonlocal transport
            if transport is None:
                transport = resolver(account)
            return transport.call(method, path, body=body, revision=revision)

        ctx.obj = {**(ctx.obj or {}), "call": call, "label": account}
        return orig_callback(*args, **kwargs)

    return click.Command(
        name=cmd.name,
        params=[account_param] + list(cmd.params),
        callback=callback,
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
        context_settings=cmd.context_settings,
        no_args_is_help=cmd.no_args_is_help,
        hidden=cmd.hidden,
        deprecated=cmd.deprecated,
    )


def build_host_group(
    resolver: Callable[[str], object],
    name: str = "klaviyo",
    extra_commands: list | None = None,
) -> click.Group:
    """Build a new top-level group exposing every command on ``main`` wrapped
    via ``wrap_with_account``, plus any host-specific ``extra_commands``.
    """

    @click.group(name=name)
    @click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
    @click.pass_context
    def group(ctx, use_json):
        ctx.obj = {"json": use_json}

    for cmd_name in sorted(main.commands):
        group.add_command(wrap_with_account(main.commands[cmd_name], resolver))
    for cmd in extra_commands or []:
        group.add_command(cmd)
    return group

"""Raw API pass-through command: api."""

import json as json_module

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError

# ---------------------------------------------------------------------------
# api (raw pass-through) — keep last
# ---------------------------------------------------------------------------


@main.command("api")
@click.argument("method")
@click.argument("path")
@click.option("--body", default=None, help="JSON body for POST/PATCH (string or @filepath)")
@click.pass_context
def api_passthrough(ctx, method, path, body):
    """Raw API pass-through: klaviyo api <METHOD> <path>."""
    try:
        parsed_body = None
        if body:
            if body.startswith("@"):
                with open(body[1:]) as f:
                    parsed_body = json_module.load(f)
            else:
                parsed_body = json_module.loads(body)
        data = ctx.obj["call"](method.upper(), path, body=parsed_body)
        output(data, use_json=True)
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

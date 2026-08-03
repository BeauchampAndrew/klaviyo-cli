"""CLI group: `klaviyo` command. Commands live in klaviyo_cli/commands/."""

from importlib.metadata import entry_points

import click

from .config import resolve_transport


def build_context(profile: str | None) -> dict:
    """Build ctx.obj auth pieces. Transport resolves lazily on first call."""
    transport = None

    def call(method, path, body=None, revision=None):
        nonlocal transport
        if transport is None:
            transport = resolve_transport(profile)
        return transport.call(method, path, body=body, revision=revision)

    return {"call": call, "label": profile or "default"}


@click.group()
@click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
@click.option("-p", "--profile", envvar="KLAVIYO_PROFILE", default=None,
              help="Named account profile from ~/.config/klaviyo-cli/config.toml")
@click.version_option(package_name="klaviyo-cli")
@click.pass_context
def main(ctx, use_json, profile):
    """Unofficial Klaviyo CLI: campaigns, segments, flows, metrics, scheduling.

    Auth: set KLAVIYO_API_KEY, or use --profile with a config file.
    Not affiliated with or endorsed by Klaviyo, Inc.
    """
    ctx.obj = {"json": use_json, **build_context(profile)}


def entry():
    """Console-script entry. Host packages (klaviyo_cli.hosts entry points)
    may supply a replacement group (e.g. an agency wrapper with per-client auth)."""
    for ep in entry_points(group="klaviyo_cli.hosts"):
        group = ep.load()()
        if group is not None:
            return group()
    return main()


from . import commands  # noqa: E402,F401  (registers subcommands on `main`)

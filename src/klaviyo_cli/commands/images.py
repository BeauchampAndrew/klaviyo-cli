"""Image commands: upload-image."""

import mimetypes
import os

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError


@main.command("upload-image")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--name", default=None, help="Name in the image library (defaults to the filename)")
@click.option("--hidden", is_flag=True, help="Hide it from the image library picker")
@click.pass_context
def upload_image(ctx, path, name, hidden):
    """Upload a local image to the account's image library and print its hosted URL.

    Use the printed image_url as an <img src> in campaign HTML (see
    set-campaign-html). Needs a transport that can send multipart uploads;
    JSON-only proxies get a clear error.
    """
    use_json = ctx.obj["json"]
    filename = os.path.basename(path)
    mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        content = fh.read()
    try:
        data = ctx.obj["upload"](
            "/api/image-upload/",
            files={"file": (filename, content, mimetype)},
            data={"name": name or filename, "hidden": "true" if hidden else "false"},
        )
        if use_json:
            output(data, use_json=True)
            return
        item = data.get("data") or {}
        attrs = item.get("attributes") or {}
        print(f"Uploaded {filename} to {ctx.obj['label']}")
        print(f"  Image ID: {item.get('id', '?')}")
        print(f"  Name: {attrs.get('name') or name or filename}")
        print(f"  URL: {attrs.get('image_url', '?')}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

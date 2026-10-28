"""edit-template: change copy, links, or images in an email without opening the editor.

Works on both kinds of email template:
  - drag-and-drop (SYSTEM_DRAGGABLE): edits the block definition. Copy lives in
    text/button blocks' `content`, links in `href`, images in image blocks.
  - HTML (CODE): edits the HTML itself.

Drag-and-drop limits, all Klaviyo's:
  - Every image block needs an image-library asset_id next to its src, so a new
    image is imported into the library first (a URL, or a local file sent as a
    data URI, which also works through JSON-only proxies).
  - `cropping_properties` is rejected on update, so it's stripped: cropped
    images still render cropped, but the crop can't be re-adjusted in the editor.
  - Klaviyo can't load some templates at all (a header block with a logo in the
    'links' layout fails validation), so those can only be edited in the UI.
"""

import base64
import html
import json
import mimetypes
import os
import re

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError
from .campaigns import TEMPLATE_READ_REVISION, _load
from .test_send import _flow_targets

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Klaviyo's import limit
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_INVISIBLE = re.compile("[\u00ad\u034f\u200b-\u200d\u2060\ufeff]")
_MISSING_ASSET = re.compile(r"Asset with id (\S+) not found")
MAX_ASSET_REPAIRS = 25


def _attr_re(name: str):
    return re.compile(rf"""\s{name}\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+)""", re.IGNORECASE)


def _attr(tag: str, name: str) -> str | None:
    m = _attr_re(name).search(tag)
    if not m:
        return None
    v = m.group(1)
    return html.unescape(v[1:-1] if v[:1] in "\"'" else v)


def _plain(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))
    text = _INVISIBLE.sub("", text)  # preheader padding
    return re.sub(r"\s+", " ", text).strip()


def _short(s: str, n: int = 90) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


# --- drag-and-drop ---------------------------------------------------------

def _nodes(definition) -> list[tuple[dict, dict]]:
    """(node, owner) for every dict in the definition, in document order. The
    owner is the nearest enclosing block or subblock (header links and logos are
    subblocks that carry content/href/src directly). Strips cropping_properties,
    which Klaviyo rejects on update."""
    found = []

    def walk(o, owner):
        if isinstance(o, dict):
            o.pop("cropping_properties", None)
            if isinstance(o.get("type"), str) and "id" in o:
                owner = o
            found.append((o, owner))
            for v in o.values():
                walk(v, owner)
        elif isinstance(o, list):
            for v in o:
                walk(v, owner)

    walk(definition, {})
    return found


def _image_nodes(definition) -> list[tuple[dict, dict]]:
    return [(n, owner) for n, owner in _nodes(definition) if isinstance(n.get("src"), str)]


def _drag_listing(definition) -> tuple[list, list]:
    texts = [{"type": owner.get("type"), "text": _plain(n["content"])}
             for n, owner in _nodes(definition) if isinstance(n.get("content"), str) and n["content"]]
    images = [{"type": owner.get("type"), "src": n["src"], "alt": n.get("alt_text"), "href": n.get("href"),
               "dynamic": bool(n.get("dynamic"))} for n, owner in _image_nodes(definition)]
    return texts, images


def _replace_in(s: str, old: str, new: str) -> tuple[str, int]:
    """Replace OLD as typed and its HTML-escaped form (& is often stored as &amp;,
    e.g. in hrefs, while the same text appears raw elsewhere)."""
    total = 0
    pairs = [(old, new)]
    esc_old = html.escape(old, quote=False)
    if esc_old != old:
        pairs.append((esc_old, html.escape(new, quote=False)))
    for o, n in pairs:
        total += s.count(o)
        s = s.replace(o, n)
    return s, total


def _code_replace(page: str, old: str, new: str) -> tuple[str, int, list[str]]:
    """Replace in an HTML page outside <head>, <style> and <script>, so CSS and
    head markup aren't touched. Also returns a few match contexts for --dry-run."""
    parts = re.split(r"(?is)(<head\b.*?</head>|<style\b.*?</style>|<script\b.*?</script>)", page)
    total, contexts = 0, []
    for i in range(0, len(parts), 2):  # odd indexes are the skipped regions
        for o in {old, html.escape(old, quote=False)}:
            for m in re.finditer(re.escape(o), parts[i]):
                if len(contexts) < 3:
                    snippet = parts[i][max(0, m.start() - 60): m.end() + 60]
                    snippet = re.sub(r"<[^>]*$", "", re.sub(r"^[^<]*?>", "", snippet))  # cut tags
                    contexts.append(_plain(snippet) or o)
        parts[i], n = _replace_in(parts[i], old, new)
        total += n
    return "".join(parts), total, contexts


def _drag_replace(definition, old: str, new: str) -> int:
    """Replace in copy (`content`) and links (`href`) anywhere. Returns the count."""
    total = 0
    for n, _ in _nodes(definition):
        for key in ("content", "href"):
            if isinstance(n.get(key), str):
                n[key], count = _replace_in(n[key], old, new)
                total += count
    return total


def _drag_swap(definition, num: int, src: str, asset_id: str, alt: str | None):
    node, owner = _image_nodes(definition)[num - 1]
    node.update({"src": src, "asset_id": asset_id})
    if "dynamic" in node:
        node["dynamic"] = False
    if alt is not None:
        node["alt_text"] = alt
    # The old image's pixel size would otherwise stick; Klaviyo sizes it from the file.
    for holder in (node, (owner.get("data") or {}).get("styles") or {}):
        holder.pop("width", None)
        holder.pop("height", None)


# --- HTML -----------------------------------------------------------------

def _code_listing(page: str) -> tuple[list, list]:
    body = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", " ", page or "")
    blocks = re.split(r"(?i)<(?:/p|/h[1-6]|/td|/div|/li|br\s*/?)>", body)
    texts = [{"type": "html", "text": t} for t in (_plain(b) for b in blocks) if t]
    images = [{"src": _attr(t, "src"), "alt": _attr(t, "alt"), "href": None, "dynamic": False}
              for t in _IMG_TAG.findall(page or "")]
    return texts, images


def _set_attr(tag: str, name: str, value: str) -> str:
    quoted = f'{name}="{html.escape(value)}"'
    pattern = _attr_re(name)
    if pattern.search(tag):
        return pattern.sub(lambda _: " " + quoted, tag, count=1)
    return re.sub(r"\s*/?>$", lambda m: f" {quoted}{m.group(0)}", tag, count=1)


def _code_swap(page: str, num: int, src: str, alt: str | None) -> str:
    m = list(_IMG_TAG.finditer(page))[num - 1]
    tag = _set_attr(m.group(0), "src", src)
    if alt is not None:
        tag = _set_attr(tag, "alt", alt)
    return page[: m.start()] + tag + page[m.end():]


# --- shared ---------------------------------------------------------------

def _import_image(call, source: str, hidden: bool = False) -> dict:
    """Add an image to the account's library from a URL or a local file."""
    if os.path.isfile(source):
        mimetype = mimetypes.guess_type(source)[0]
        if mimetype not in IMAGE_TYPES:
            raise click.ClickException(f"{source}: Klaviyo takes JPEG, PNG or GIF images.")
        raw = open(source, "rb").read()
        if len(raw) > MAX_IMAGE_BYTES:
            raise click.ClickException(f"{source} is over Klaviyo's 5 MB image limit.")
        url, name = f"data:{mimetype};base64,{base64.b64encode(raw).decode()}", os.path.basename(source)
    elif source.startswith(("http://", "https://")):
        url, name = source, source.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "image"
    else:
        raise click.ClickException(f"{source} is neither a local file nor an http(s) URL.")
    data = call("POST", "/api/images/", body={"data": {"type": "image", "attributes": {
        "name": name, "import_from_url": url, "hidden": hidden}}}).get("data") or {}
    src = (data.get("attributes") or {}).get("image_url")
    if not (data.get("id") and src):
        raise click.ClickException(f"Klaviyo didn't return a library image for {source}.")
    return {"asset_id": data.get("id"), "src": src}


def _save(call, tid: str, attrs: dict) -> list[str]:
    """PATCH the template. Klaviyo rejects a drag-and-drop save if any image's
    asset_id isn't in this account's library, which happens to every template
    copied in from another account. Re-import each such image from its URL and
    retry. Returns notes on what was re-imported."""
    notes = []
    for _ in range(MAX_ASSET_REPAIRS + 1):
        try:
            call("PATCH", f"/api/templates/{tid}/", body={"data": {
                "type": "template", "id": tid, "attributes": attrs}})
            return notes
        except APIError as e:
            m = _MISSING_ASSET.search(str(e))
            if not m or "definition" not in attrs:
                raise
            nodes = [n for n, _ in _image_nodes(attrs["definition"]) if str(n.get("asset_id")) == m.group(1)]
            if not nodes:
                raise
            old_src = nodes[0]["src"]
            hosted = _import_image(call, old_src, hidden=True)
            for n in nodes:
                n.update(hosted)
            notes.append(f"Re-imported an image from another account's library ({old_src})")
    raise click.ClickException(f"Gave up after re-importing {MAX_ASSET_REPAIRS} images from other accounts.")


def _resolve(call, template_id, campaign_id, flow_id, email_num) -> dict:
    """{template_id, label, warning} for whichever target was given."""
    if template_id:
        return {"template_id": template_id, "label": f"template {template_id}", "warning": None}
    if campaign_id:
        view = _load(call, campaign_id)
        emails = [v for v in view["variations"] if v.get("channel") == "email" and v.get("template_id")]
        if not emails:
            raise click.ClickException(f"Campaign {view['id']} has no email template.")
        if len(emails) > 1:
            ids = ", ".join(v["template_id"] for v in emails)
            raise click.ClickException(
                f"Campaign {view['id']} has {len(emails)} email variations; pick one with "
                f"--template ({ids}).")
        status = (view.get("status") or "").lower()
        warning = None
        if status in ("sent", "sending", "cancelled"):
            raise click.ClickException(f"Campaign {view['id']} is {status}; there's nothing left to edit.")
        if status == "scheduled":
            warning = "This campaign is scheduled; the edit goes out with it."
        return {"template_id": emails[0]["template_id"], "label": view.get("name") or view["id"],
                "warning": warning}
    try:
        num = int(email_num)
    except (TypeError, ValueError):
        raise click.UsageError("--flow needs --email with ONE email number, as flow-detail shows it.")
    target = _flow_targets(call, flow_id, str(num))[0]
    if not target["template_id"]:
        raise click.ClickException(f"{target['label']} in flow {flow_id} has no template yet.")
    status = ((call("GET", f"/api/flows/{flow_id}/?fields[flow]=status").get("data") or {})
              .get("attributes") or {}).get("status")
    warning = "This flow is live; the edit applies to the next email it sends." if status == "live" else None
    return {"template_id": target["template_id"], "label": target["label"], "warning": warning}


def _read(call, template_id: str) -> dict:
    """The template's editor type and HTML (read at a revision that can open
    every template), plus the drag-and-drop definition when there is one."""
    attrs = (call("GET", f"/api/templates/{template_id}/", revision=TEMPLATE_READ_REVISION)
             .get("data") or {}).get("attributes") or {}
    tpl = {"name": attrs.get("name"), "editor_type": attrs.get("editor_type"), "html": attrs.get("html") or ""}
    tpl["had_crops"] = False
    if tpl["editor_type"] == "SYSTEM_DRAGGABLE":
        try:
            full = call("GET", f"/api/templates/{template_id}/?additional-fields[template]=definition")
        except APIError as e:
            if "Failed to load definition" in str(e):
                raise click.ClickException(
                    f"Klaviyo's API can't open template {template_id} for editing ({e}). "
                    "Edit this one in the Klaviyo editor.")
            raise
        tpl["definition"] = ((full.get("data") or {}).get("attributes") or {}).get("definition")
        tpl["had_crops"] = '"cropping_properties"' in json.dumps(tpl["definition"])
    elif tpl["editor_type"] != "CODE":
        raise click.ClickException(
            f"Template {template_id} uses Klaviyo's {tpl['editor_type']} editor; edit-template "
            "handles drag-and-drop and HTML templates only.")
    return tpl


@main.command("edit-template")
@click.argument("template_id", required=False)
@click.option("--campaign", "campaign_id", default=None, help="Edit a campaign's email instead")
@click.option("--flow", "flow_id", default=None, help="Edit a flow email instead (with --email)")
@click.option("--email", "email_num", default=None, help="With --flow: the email number flow-detail shows")
@click.option("--replace", "replacements", nargs=2, multiple=True, metavar="OLD NEW",
              help="Replace copy or a link everywhere it appears (repeatable)")
@click.option("--image", "images", nargs=2, multiple=True, metavar="N SOURCE",
              help="Swap image N (from the listing) for a URL or local file (repeatable)")
@click.option("--alt", "alts", nargs=2, multiple=True, metavar="N TEXT", help="Set image N's alt text")
@click.option("--dry-run", is_flag=True, help="Show what would change without saving")
@click.pass_context
def edit_template(ctx, template_id, campaign_id, flow_id, email_num, replacements, images, alts, dry_run):
    """Change copy, links, or images in an email. With no edits, lists them.

    \b
      klaviyo edit-template --campaign <id>                  # list copy + numbered images
      klaviyo edit-template --campaign <id> --replace "Ends Friday" "Ends Sunday"
      klaviyo edit-template --flow TCAqyz --email 2 --image 3 ./new-hero.png --alt 3 "New hero"
      klaviyo edit-template UpRZrn --replace "?utm=old" "?utm=new" --dry-run

    --replace matches the text as stored, so a phrase broken up by formatting
    (e.g. half bold) won't match; use a shorter piece of it. Every match is
    replaced. For drag-and-drop emails it also reaches button and image links.
    """
    use_json = ctx.obj["json"]
    if len([x for x in (template_id, campaign_id, flow_id) if x]) != 1:
        raise click.UsageError("Give exactly one of: a template ID, --campaign, or --flow.")
    if email_num and not flow_id:
        raise click.UsageError("--email only applies with --flow.")
    try:
        nums = {"image": [int(n) for n, _ in images], "alt": [int(n) for n, _ in alts]}
    except ValueError:
        raise click.UsageError("--image and --alt take an image number first (from the listing).")
    try:
        call = ctx.obj["call"]
        target = _resolve(call, template_id, campaign_id, flow_id, email_num)
        tid = target["template_id"]
        tpl = _read(call, tid)
        if template_id:
            target["label"] = tpl["name"] or target["label"]
        drag = tpl["editor_type"] == "SYSTEM_DRAGGABLE"
        texts, image_list = _drag_listing(tpl["definition"]) if drag else _code_listing(tpl["html"])

        if not (replacements or images or alts):
            if use_json:
                output({**target, "template_id": tid, "editor_type": tpl["editor_type"],
                        "texts": texts, "images": image_list}, use_json=True)
                return
            kind = "drag-and-drop" if drag else "HTML"
            print(f"{target['label']}  (template {tid}, {kind})")
            print("\nCopy:")
            for t in texts:
                print(f"  [{t['type']}] {_short(t['text'])}")
            print("\nImages:")
            for i, im in enumerate(image_list, 1):
                extra = "  (dynamic)" if im["dynamic"] else ""
                if im.get("type") and im["type"] != "image":
                    extra += f"  ({im['type'].replace('_', ' ')})"
                print(f"  {i}. {im['alt'] or '(no alt)'}  {im['src'] or '?'}{extra}")
            if not image_list:
                print("  (none)")
            return

        bad = [n for n in nums["image"] + nums["alt"] if not 1 <= n <= len(image_list)]
        if bad:
            raise click.ClickException(
                f"No image {bad[0]}; this email has {len(image_list)} (run without edits to list them).")

        changes, page = [], tpl["html"]
        for old, new in replacements:
            if drag:
                n = _drag_replace(tpl["definition"], old, new)
            else:
                page, n, where = _code_replace(page, old, new)
            if not n:
                raise click.ClickException(
                    f'"{old}" isn\'t in this email (nothing was saved). If it is, formatting may split '
                    "it up; try a shorter piece.")
            changes.append(f'Replaced "{_short(old, 50)}" with "{_short(new, 50)}" ({n}x)')
            if dry_run and not drag:
                changes += [f"    ...{_short(w, 100)}..." for w in where]

        alt_for = {int(n): text for n, text in alts}
        swapped = []
        for n_str, source in images:
            n = int(n_str)
            if dry_run:
                changes.append(f"Image {n}: {image_list[n - 1]['src']} -> {source}")
                continue
            hosted = _import_image(call, source)
            alt = alt_for.pop(n, None)
            if drag:
                _drag_swap(tpl["definition"], n, hosted["src"], hosted["asset_id"], alt)
            else:
                page = _code_swap(page, n, hosted["src"], alt)
            swapped.append(hosted["src"])
            changes.append(f"Image {n}: now {hosted['src']}" + (f' (alt "{alt}")' if alt is not None else ""))
        for n, text in alt_for.items():
            if drag:
                _image_nodes(tpl["definition"])[n - 1][0]["alt_text"] = text
            else:
                m = list(_IMG_TAG.finditer(page))[n - 1]
                page = page[: m.start()] + _set_attr(m.group(0), "alt", text) + page[m.end():]
            changes.append(f'Image {n}: alt text "{text}"')

        if dry_run:
            if use_json:
                output({**target, "dry_run": True, "changes": changes}, use_json=True)
                return
            print(f"{target['label']}  (template {tid}) - dry run, nothing saved:")
            for c in changes:
                print(f"  {c}")
            return

        attrs = {"definition": tpl["definition"]} if drag else {"html": page}
        changes += _save(call, tid, attrs)

        live = ((call("GET", f"/api/templates/{tid}/", revision=TEMPLATE_READ_REVISION)
                 .get("data") or {}).get("attributes") or {}).get("html") or ""
        problems = [f"new image {s} isn't in the saved email" for s in swapped if s not in live]
        live_text = _plain(live)
        for old, new in replacements:
            if new and _plain(new) not in live_text and html.escape(new, quote=False) not in live and new not in live:
                problems.append(f'"{_short(new, 50)}" isn\'t in the saved email')

        if use_json:
            output({**target, "changes": changes, "problems": problems}, use_json=True)
        else:
            print(f"Saved {target['label']}  (template {tid}):")
            for c in changes:
                print(f"  {c}")
            if target["warning"]:
                print(f"  Note: {target['warning']}")
            if tpl["had_crops"]:
                print("  Note: images cropped in the editor still show cropped, but the crop "
                      "can't be re-adjusted there anymore (Klaviyo's API drops crop settings).")
        if problems:
            raise click.ClickException("Saved, but the check found problems: " + "; ".join(problems))
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

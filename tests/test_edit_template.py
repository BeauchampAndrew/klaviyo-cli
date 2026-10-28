import copy
import json
from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main
from klaviyo_cli.commands.campaigns import TEMPLATE_READ_REVISION
from klaviyo_cli.transport import APIError
from tests.test_campaigns import _api, _message


def _block(btype, data, **extra):
    return {"content_type": "block", "type": btype, "id": f"id-{btype}", "data": data, **extra}


DEFINITION = {"body": {"sections": [{"type": "section", "id": "s1", "rows": [{"columns": [{"blocks": [
    _block("header", {"styles": {}}, subblocks=[
        {"type": "header_link_bar_logo", "id": "logo", "src": "https://cdn/logo.png", "asset_id": "11",
         "alt_text": "Logo", "width": 430, "height": 138},
        {"type": "header_link_bar_button", "id": "nav", "content": "SHOP", "href": "https://x.com/all"},
    ]),
    _block("image", {"properties": {"src": "https://cdn/hero.png", "asset_id": "22", "alt_text": "Hero",
                                    "href": "https://x.com/all", "dynamic": False},
                     "styles": {"width": 1200, "height": 900, "max_width": 600},
                     "cropping_properties": {"x": 1}}),
    _block("text", {"content": "<p>Sale ends <b>Friday</b> &amp; more</p>"}),
    _block("button", {"content": "SHOP NOW", "properties": {"href": "https://x.com/all"}}),
]}]}]}]}}


def _drag_routes(definition=None, patch_resp=None, extra=()):
    definition = copy.deepcopy(definition or DEFINITION)
    return [
        *extra,
        (("GET", "/api/templates/T1/?additional-fields"), {"data": {"attributes": {"definition": definition}}}),
        (("GET", "/api/templates/T1/"), {"data": {"attributes": {
            "name": "Promo", "editor_type": "SYSTEM_DRAGGABLE",
            "html": "<p>Sale ends Sunday & more</p> SHOP NOW https://x.com/best https://cdn/new.png"}}}),
        (("PATCH", "/api/templates/T1/"), patch_resp if patch_resp is not None else {}),
        (("POST", "/api/images/"), {"data": {"id": "99", "attributes": {"image_url": "https://cdn/new.png"}}}),
    ]


def _run(routes, *args):
    ctx_obj, calls = _api(routes)
    with patch("klaviyo_cli.cli.build_context", return_value=ctx_obj):
        result = CliRunner().invoke(main, ["edit-template", *args])
    return result, calls


def _patched(calls):
    return [c[2]["data"]["attributes"] for c in calls if c[0] == "PATCH"]


def test_listing_numbers_every_image_including_header_logos():
    result, calls = _run(_drag_routes(), "T1")
    assert result.exit_code == 0, result.output
    assert "1. Logo  https://cdn/logo.png  (header link bar logo)" in result.output
    assert "2. Hero  https://cdn/hero.png" in result.output
    assert "[header_link_bar_button] SHOP" in result.output
    assert "[text] Sale ends Friday & more" in result.output
    assert not _patched(calls)
    # The plain read goes through the old revision, which can open every template.
    assert calls[0][3] == TEMPLATE_READ_REVISION


def test_replace_reaches_copy_buttons_and_every_link_and_strips_crops():
    result, calls = _run(_drag_routes(), "T1", "--replace", "Friday", "Sunday",
                         "--replace", "/all", "/best")
    assert result.exit_code == 0, result.output
    saved = json.dumps(_patched(calls)[0]["definition"])
    assert "Sunday" in saved and "Friday" not in saved
    assert "/all" not in saved and saved.count("/best") == 3  # header link, image link, button
    assert "cropping_properties" not in saved
    assert '"/all" with "/best" (3x)' in result.output
    assert "crop" in result.output  # warned that crops can't be re-adjusted


def test_replace_matches_escaped_ampersands():
    result, calls = _run(_drag_routes(), "T1", "--replace", "& more", "& more")
    assert result.exit_code == 0, result.output
    assert "</b> &amp; more" in json.dumps(_patched(calls)[0]["definition"])


def test_text_not_found_saves_nothing():
    result, calls = _run(_drag_routes(), "T1", "--replace", "Friday", "Sunday",
                         "--replace", "Black Friday", "x")
    assert result.exit_code != 0
    assert "isn't in this email (nothing was saved)" in result.output
    assert not _patched(calls)


def test_image_swap_imports_the_file_and_drops_the_old_size(tmp_path):
    img = tmp_path / "new.png"
    img.write_bytes(b"\x89PNG fake")
    result, calls = _run(_drag_routes(), "T1", "--image", "2", str(img), "--alt", "2", "New hero")
    assert result.exit_code == 0, result.output
    upload = next(c for c in calls if c[1] == "/api/images/")[2]["data"]["attributes"]
    assert upload["import_from_url"].startswith("data:image/png;base64,")
    assert upload["hidden"] is False
    image = _patched(calls)[0]["definition"]["body"]["sections"][0]["rows"][0]["columns"][0]["blocks"][1]
    props = image["data"]["properties"]
    assert (props["src"], props["asset_id"], props["alt_text"]) == ("https://cdn/new.png", "99", "New hero")
    assert image["data"]["styles"] == {"max_width": 600}


def test_header_logo_swap_updates_the_subblock_itself():
    result, calls = _run(_drag_routes(), "T1", "--image", "1", "https://example.com/logo2.png")
    assert result.exit_code == 0, result.output
    logo = _patched(calls)[0]["definition"]["body"]["sections"][0]["rows"][0]["columns"][0]["blocks"][0][
        "subblocks"][0]
    assert logo["src"] == "https://cdn/new.png" and logo["asset_id"] == "99"
    assert "width" not in logo and "height" not in logo


def test_dry_run_imports_and_saves_nothing():
    result, calls = _run(_drag_routes(), "T1", "--replace", "Friday", "Sunday",
                         "--image", "2", "https://example.com/x.png", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "dry run, nothing saved" in result.output
    assert not _patched(calls)
    assert not [c for c in calls if c[1] == "/api/images/"]


def test_image_number_out_of_range():
    result, _ = _run(_drag_routes(), "T1", "--image", "3", "https://example.com/x.png")
    assert result.exit_code != 0
    assert "No image 3; this email has 2" in result.output


def test_images_from_another_account_are_reimported_then_the_save_is_retried():
    """Klaviyo rejects the save while any asset_id belongs to another account's
    library; the image is re-imported (hidden) from its URL and the save retried."""
    routes = _drag_routes(patch_resp=[APIError("Asset with id 22 not found."), {}])
    result, calls = _run(routes, "T1", "--replace", "Friday", "Sunday")
    assert result.exit_code == 0, result.output
    first, second = _patched(calls)
    upload = next(c for c in calls if c[1] == "/api/images/")[2]["data"]["attributes"]
    assert upload["import_from_url"] == "https://cdn/hero.png" and upload["hidden"] is True
    assert '"asset_id": "99"' in json.dumps(second["definition"])
    assert "Re-imported an image from another account's library (https://cdn/hero.png)" in result.output


def test_template_klaviyo_cannot_load_says_to_use_the_editor():
    routes = [(("GET", "/api/templates/T1/?additional-fields"), APIError(
        "Failed to load definition for template T1: Header blocks with 'links' desktop layout "
        "must not include a logo subblock @ $")), *_drag_routes()]
    result, calls = _run(routes, "T1", "--replace", "Friday", "Sunday")
    assert result.exit_code != 0
    assert "Edit this one in the Klaviyo editor" in result.output
    assert not _patched(calls)


CODE_HTML = ('<html><head><style>p{}</style></head><body><p>Hi &amp; welcome͏͏</p>'
             '<a href="https://x.com/a"><img src="https://cdn/a.png" alt="first" width="600"/></a>'
             "<img alt='old' src=\"https://cdn/b.png\"></body></html>")


def _code_routes(saved=None):
    def tpl(html):
        return {"data": {"attributes": {"name": "Code", "editor_type": "CODE", "html": html}}}
    return [
        (("GET", "/api/templates/T2/"), [tpl(CODE_HTML), tpl(saved or CODE_HTML)]),
        (("PATCH", "/api/templates/T2/"), {}),
        (("POST", "/api/images/"), {"data": {"id": "99", "attributes": {"image_url": "https://cdn/new.png"}}}),
    ]


def test_html_template_listing_and_edits():
    result, _ = _run(_code_routes(), "T2")
    assert result.exit_code == 0, result.output
    assert "[html] Hi & welcome" in result.output and "͏" not in result.output
    assert "2. old  https://cdn/b.png" in result.output

    result, calls = _run(_code_routes(saved="Hello &amp; welcome https://cdn/new.png"), "T2", "--replace", "Hi & welcome", "Hello & welcome",
                         "--image", "2", "https://example.com/x.png", "--alt", "2", "new")
    assert result.exit_code == 0, result.output
    html = _patched(calls)[0]["html"]
    assert "Hello &amp; welcome" in html
    # The single-quoted alt is replaced, not duplicated; the first image is untouched.
    assert '<img alt="new" src="https://cdn/new.png">' in html
    assert 'src="https://cdn/a.png"' in html


def test_sent_campaign_is_refused():
    routes = [(("GET", "/api/campaign-messages/MSG1/"), _message(status="Sent"))]
    result, calls = _run(routes, "--campaign", "MSG1", "--replace", "a", "b")
    assert result.exit_code != 0
    assert "is sent; there's nothing left to edit" in result.output
    assert not _patched(calls)


def test_html_replace_changes_raw_and_escaped_forms_but_not_css():
    page = ('<html><head><style>.Friday{color:red}</style></head><body>'
            '<p>Shop ?a=1&b=2 Friday</p><a href="https://x.com/?a=1&amp;b=2">go</a></body></html>')
    routes = [(("GET", "/api/templates/T3/"), [
        {"data": {"attributes": {"name": "C", "editor_type": "CODE", "html": page}}},
        {"data": {"attributes": {"name": "C", "editor_type": "CODE", "html": "?a=9&amp;b=2 Sunday"}}}]),
        (("PATCH", "/api/templates/T3/"), {})]
    result, calls = _run(routes, "T3", "--replace", "?a=1&b=2", "?a=9&b=2", "--replace", "Friday", "Sunday")
    assert result.exit_code == 0, result.output
    html = _patched(calls)[0]["html"]
    assert "a=1" not in html and "?a=9&b=2" in html and "?a=9&amp;b=2" in html
    assert ".Friday{" in html and "Shop ?a=9&b=2 Sunday" in html
    assert '"?a=1&b=2" with "?a=9&b=2" (2x)' in result.output


def test_unquoted_image_attributes_are_replaced_not_duplicated():
    page = "<body><img src=https://cdn/a.png alt=old></body>"
    routes = [(("GET", "/api/templates/T4/"), [
        {"data": {"attributes": {"name": "C", "editor_type": "CODE", "html": page}}},
        {"data": {"attributes": {"name": "C", "editor_type": "CODE", "html": "https://cdn/new.png"}}}]),
        (("PATCH", "/api/templates/T4/"), {}),
        (("POST", "/api/images/"), {"data": {"id": "9", "attributes": {"image_url": "https://cdn/new.png"}}})]
    result, calls = _run(routes, "T4", "--image", "1", "https://example.com/n.png", "--alt", "1", "new")
    assert result.exit_code == 0, result.output
    assert _patched(calls)[0]["html"] == '<body><img src="https://cdn/new.png" alt="new"></body>'

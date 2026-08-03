from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main


def _fake_ctx_factory(pages):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body))
        return pages.pop(0)

    return {"call": call, "label": "test-account"}, calls


def _flow(fid, name):
    return {
        "id": fid,
        "attributes": {"name": name, "status": "live", "updated": "2026-05-01T00:00:00"},
    }


@patch("klaviyo_cli.cli.build_context")
def test_flows_paginates_all_pages(mock_build):
    """flows must follow links.next so accounts with >50 flows aren't truncated."""
    page1 = {
        "data": [_flow("AAA111", "Welcome Flow")],
        "links": {"next": "https://a.klaviyo.com/api/flows/?page%5Bcursor%5D=NEXT"},
    }
    page2 = {
        "data": [_flow("BBB222", "Add To Cart")],
        "links": {"next": None},
    }
    ctx_obj, calls = _fake_ctx_factory([page1, page2])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["flows"])

    assert result.exit_code == 0, result.output
    # Both pages' flows must appear — the second page is the regression case.
    assert "AAA111" in result.output
    assert "BBB222" in result.output
    assert "(2 total)" in result.output
    # It must actually have made the second (cursor) request.
    assert len(calls) == 2

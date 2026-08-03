from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main


def _fake_ctx_factory(pages):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body))
        return pages.pop(0)

    return {"call": call, "label": "test-account"}, calls


def _metric(mid, name, integration="Klaviyo"):
    return {"id": mid, "attributes": {"name": name, "integration": {"name": integration}}}


_METRICS_PAGE = {
    "data": [
        _metric("TY5Ysg", "Opened Email"),
        _metric("XJcga2", "Active on Site", "API"),
        _metric("RHTyZW", "Clicked Email"),
    ],
    "links": {"next": None},
}


@patch("klaviyo_cli.cli.build_context")
def test_list_metrics_resolves_and_filters(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_METRICS_PAGE])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["list-metrics", "--search", "email"])

    assert result.exit_code == 0, result.output
    assert "Opened Email" in result.output
    assert "TY5Ysg" in result.output
    assert "Clicked Email" in result.output
    # "Active on Site" filtered out by the --search term.
    assert "Active on Site" not in result.output

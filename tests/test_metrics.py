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


# ---------------------------------------------------------------------------
# metric-aggregate
# ---------------------------------------------------------------------------


@patch("klaviyo_cli.cli.build_context")
def test_metric_aggregate_posts_body_and_renders_table(mock_build):
    resp = {"data": {"attributes": {
        "dates": ["2026-07-06T00:00:00+00:00", "2026-07-13T00:00:00+00:00"],
        "data": [{"dimensions": [],
                  "measurements": {"count": [1881, 1937], "unique": [439, 409]}}],
    }}}
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "metric-aggregate", "RHUudM",
        "--since", "2026-07-01", "--until", "2026-07-31",
        "--timezone", "America/Chicago",
    ])

    assert result.exit_code == 0, result.output
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/api/metric-aggregates/"
    attrs = body["data"]["attributes"]
    assert attrs["metric_id"] == "RHUudM"
    assert attrs["measurements"] == ["count", "unique"]
    assert attrs["interval"] == "week"
    assert attrs["timezone"] == "America/Chicago"
    assert attrs["filter"][0] == "greater-or-equal(datetime,2026-07-01T00:00:00)"
    assert "by" not in attrs
    assert "2026-07-06" in result.output
    assert "1,881" in result.output
    # Totals row.
    assert "TOTAL" in result.output
    assert "3,818" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_metric_aggregate_by_renders_dimension_blocks(mock_build):
    resp = {"data": {"attributes": {
        "dates": ["2026-07-06T00:00:00+00:00"],
        "data": [
            {"dimensions": ["Yoder Smokers"], "measurements": {"count": [7]}},
            {"dimensions": ["Ooni"], "measurements": {"count": [3]}},
        ],
    }}}
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "metric-aggregate", "MET1", "--measurements", "count", "--by", "Name",
    ])

    assert result.exit_code == 0, result.output
    assert calls[0][2]["data"]["attributes"]["by"] == ["Name"]
    assert "Yoder Smokers" in result.output
    assert "Ooni" in result.output

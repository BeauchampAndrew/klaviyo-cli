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


# ---------------------------------------------------------------------------
# forms
# ---------------------------------------------------------------------------


def _form(fid, name, status, updated, created="2026-01-01T00:00:00+00:00"):
    return {"id": fid, "attributes": {
        "name": name, "status": status, "created_at": created, "updated_at": updated}}


_FORMS_PAGE_1 = {
    "data": [
        _form("OLD111", "Newsletter Signup", "live", "2020-07-15T00:00:00+00:00"),
        _form("NEW222", "Welcome Pop-Up", "draft", "2026-10-16T19:57:22+00:00"),
    ],
    "links": {"next": "https://a.klaviyo.com/api/forms/?page[cursor]=abc"},
}
_FORMS_PAGE_2 = {
    "data": [_form("MID333", "Birthday Offer", "live", "2026-10-14T00:00:00+00:00")],
    "links": {"next": None},
}


@patch("klaviyo_cli.cli.build_context")
def test_forms_paginates_and_sorts_newest_first(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_FORMS_PAGE_1, _FORMS_PAGE_2])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["forms"])

    assert result.exit_code == 0, result.output
    assert calls[1][1] == "/api/forms/?page[cursor]=abc"
    out = result.output
    assert out.index("Welcome Pop-Up") < out.index("Birthday Offer") < out.index("Newsletter Signup")
    assert "NEW222" in out and "draft" in out


@patch("klaviyo_cli.cli.build_context")
def test_forms_status_filter(mock_build):
    ctx_obj, _ = _fake_ctx_factory([_FORMS_PAGE_1, _FORMS_PAGE_2])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["forms", "--status", "live"])

    assert result.exit_code == 0, result.output
    assert "Birthday Offer" in result.output
    assert "Newsletter Signup" in result.output
    assert "Welcome Pop-Up" not in result.output


@patch("klaviyo_cli.cli.build_context")
def test_forms_versions_fetches_each_listed_form(mock_build):
    versions = {"data": [{"id": "28840506", "attributes": {
        "form_type": "popup", "variation_name": "Copy 2 of Promo10%",
        "status": "draft", "updated_at": "2026-10-16T19:57:22+00:00"}}]}
    page = {"data": [_form("NEW222", "Welcome Pop-Up", "draft", "2026-10-16T19:57:22+00:00")],
            "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page, versions])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["forms", "--versions"])

    assert result.exit_code == 0, result.output
    assert calls[1][1] == "/api/forms/NEW222/form-versions/"
    assert "Copy 2 of Promo10%" in result.output
    assert "popup" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_form_performance_by_version_labels_ab_variations(mock_build):
    report = {"data": {"attributes": {"results": [
        {"groupings": {"form_id": "F1", "form_version_id": "12"},
         "statistics": {"viewed_form": 1000.0, "viewed_form_uniques": 900.0, "submits": 60.0, "submit_rate": 0.06}},
        {"groupings": {"form_id": "F1", "form_version_id": "11"},
         "statistics": {"viewed_form": 1000.0, "viewed_form_uniques": 880.0, "submits": 40.0, "submit_rate": 0.04}},
    ]}}}
    form = {"data": {"attributes": {"name": "Popup test"}}}
    versions = {"data": [
        {"id": "11", "attributes": {"variation_name": "Control", "ab_test": {"variation_name": "Control"},
                                    "status": "live", "created_at": "2026-10-01T00:00:00Z"}},
        {"id": "12", "attributes": {"variation_name": "10% off", "ab_test": {"variation_name": "10% off"},
                                    "status": "live", "created_at": "2026-10-01T00:00:00Z"}},
    ]}
    ctx_obj, calls = _fake_ctx_factory([report, form, versions])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["form-performance", "--form", "F1", "--by-version",
                                       "--since", "2026-10-01", "--until", "2026-10-21"])

    assert result.exit_code == 0, result.output
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/api/form-values-reports/")
    attrs = body["data"]["attributes"]
    assert attrs["group_by"] == ["form_id", "form_version_id"]
    assert attrs["filter"] == 'equals(form_id,"F1")'
    assert attrs["timeframe"] == {"start": "2026-10-01T00:00:00", "end": "2026-10-21T23:59:59"}
    assert 'A/B "Control"' in result.output and 'A/B "10% off"' in result.output
    assert "6.00%" in result.output and "4.00%" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_form_performance_without_form_flags_keeps_monthly_totals(mock_build):
    metrics = {"data": [_metric("V", "Viewed Form"), _metric("S", "submitted_form_step"),
                        _metric("U", "Subscribed to Email Marketing")]}
    agg = {"data": {"attributes": {"dates": ["2026-10-01"], "data": [{"measurements": {"count": [10]}}]}}}
    ctx_obj, calls = _fake_ctx_factory([metrics, agg, agg, agg])
    mock_build.return_value = ctx_obj
    with patch("time.sleep"):
        result = CliRunner().invoke(main, ["form-performance", "--days", "30"])
    assert result.exit_code == 0, result.output
    assert "last 30 days" in result.output
    assert all(p != "/api/form-values-reports/" for _, p, _ in calls)

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
def test_get_segment_renders_resolved_conditions(mock_build):
    seg = {
        "data": {
            "id": "SEG123",
            "attributes": {
                "name": "Test Segment",
                "profile_count": 4200,
                "definition": {
                    "condition_groups": [
                        {"conditions": [{
                            "type": "profile-metric", "metric_id": "XJcga2",
                            "measurement": "count",
                            "measurement_filter": {"type": "numeric", "operator": "equals", "value": 0},
                            "timeframe_filter": {"type": "date", "operator": "in-the-last", "unit": "day", "quantity": 30},
                        }]},
                        {"conditions": [{
                            "type": "profile-metric", "metric_id": "TY5Ysg",
                            "measurement": "count",
                            "measurement_filter": {"type": "numeric", "operator": "greater-than", "value": 0},
                            "timeframe_filter": {"type": "date", "operator": "in-the-last", "unit": "day", "quantity": 30},
                        }]},
                    ]
                },
            },
        }
    }
    ctx_obj, calls = _fake_ctx_factory([seg, _METRICS_PAGE])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-segment", "SEG123"])
    assert result.exit_code == 0, result.output
    assert "4,200" in result.output
    # Opaque metric IDs must be resolved to names with windows.
    assert "Active on Site =0 in last 30d" in result.output
    assert "Opened Email >0 in last 30d" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_search_segments_filters_by_keyword(mock_build):
    seg_list = {
        "data": [
            {"id": "S1", "attributes": {"name": "No site activity + Email Engaged", "definition": {"condition_groups": []}}},
            {"id": "S2", "attributes": {"name": "Active Subscribers", "definition": {"condition_groups": []}}},
        ],
        "links": {"next": None},
    }
    ctx_obj, calls = _fake_ctx_factory([seg_list, _METRICS_PAGE])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["search-segments", "engaged"])
    assert result.exit_code == 0, result.output
    assert "S1" in result.output
    assert "S2" not in result.output
    assert "1 of 2" in result.output


_BODY = '{"condition_groups": [{"conditions": [{"type": "profile-metric", "metric_id": "TY5Ysg", "measurement": "count", "measurement_filter": {"type": "numeric", "operator": "greater-than", "value": 0}, "timeframe_filter": {"type": "date", "operator": "in-the-last", "unit": "day", "quantity": 30}}]}]}'
_EXISTING = {
    "data": [{"id": "DUP1", "attributes": {"name": "My Segment"}}],
    "links": {"next": None},
}


@patch("klaviyo_cli.cli.build_context")
def test_create_segment_blocks_duplicate_name(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_EXISTING])  # only the dedup fetch; POST must NOT happen
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(
        main, ["create-segment", "--name", "My Segment", "--body", _BODY]
    )
    assert result.exit_code != 0
    assert "already exists" in result.output
    assert "DUP1" in result.output
    assert len(calls) == 1  # no POST


@patch("klaviyo_cli.cli.build_context")
def test_get_segment_renders_negative_consent(mock_build):
    """can_receive_marketing: false must not render as 'can receive'."""
    seg = {
        "data": {
            "id": "SEG9",
            "attributes": {
                "name": "Suppressed People",
                "profile_count": 10,
                "definition": {
                    "condition_groups": [
                        {"conditions": [{
                            "type": "profile-marketing-consent",
                            "consent": {"channel": "email", "can_receive_marketing": False},
                        }]},
                        {"conditions": [{
                            "type": "profile-marketing-consent",
                            "consent": {"channel": "email", "can_receive_marketing": True},
                        }]},
                    ]
                },
            },
        }
    }
    ctx_obj, calls = _fake_ctx_factory([seg, _METRICS_PAGE])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["get-segment", "SEG9"])
    assert result.exit_code == 0, result.output
    assert "can NOT receive email marketing (suppressed or no consent)" in result.output
    assert "2. can receive email marketing" in result.output


_SEG_CURRENT = {
    "data": {"id": "SEG1", "attributes": {
        "name": "Old Name",
        "definition": {"condition_groups": [{"conditions": [{
            "type": "profile-metric", "metric_id": "TY5Ysg", "measurement": "count",
            "measurement_filter": {"type": "numeric", "operator": "greater-than", "value": 0},
            "timeframe_filter": {"type": "date", "operator": "alltime"},
        }]}]},
    }}
}


def _seg(name, definition):
    return {"data": {"id": "SEG1", "attributes": {"name": name, "definition": definition}}}


@patch("klaviyo_cli.cli.build_context")
def test_update_segment_definition_patches_and_verifies(mock_build):
    new_def = {"condition_groups": [{"conditions": [{
        "type": "profile-metric", "metric_id": "TY5Ysg", "measurement": "count",
        "measurement_filter": {"type": "numeric", "operator": "greater-than", "value": 0},
        "timeframe_filter": {"type": "date", "operator": "in-the-last", "unit": "day", "quantity": 7},
    }]}]}
    # Read-back carries an extra null field; verification must still pass.
    read_back = {"condition_groups": [{"conditions": [
        {**new_def["condition_groups"][0]["conditions"][0], "metric_filters": None}]}]}
    ctx_obj, calls = _fake_ctx_factory([
        _SEG_CURRENT, _METRICS_PAGE, {"data": {}}, _seg("Old Name", read_back)])
    mock_build.return_value = ctx_obj
    import json
    result = CliRunner().invoke(main, ["update-segment", "SEG1", "--body", json.dumps(new_def)])
    assert result.exit_code == 0, result.output
    assert "Before: Opened Email >0" in result.output
    assert "in last 7d" in result.output
    patch_call = [c for c in calls if c[0] == "PATCH"][0]
    assert patch_call[1] == "/api/segments/SEG1/"
    assert patch_call[2]["data"]["attributes"] == {"definition": new_def}
    assert "Saved (verified)" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_update_segment_rename_only(mock_build):
    ctx_obj, calls = _fake_ctx_factory([
        _SEG_CURRENT, {"data": {}}, _seg("New Name", _SEG_CURRENT["data"]["attributes"]["definition"])])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["update-segment", "SEG1", "--name", "New Name"])
    assert result.exit_code == 0, result.output
    patch_call = [c for c in calls if c[0] == "PATCH"][0]
    assert patch_call[2]["data"]["attributes"] == {"name": "New Name"}


@patch("klaviyo_cli.cli.build_context")
def test_update_segment_dry_run_does_not_patch(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_SEG_CURRENT])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["update-segment", "SEG1", "--name", "New Name", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert not [c for c in calls if c[0] == "PATCH"]


@patch("klaviyo_cli.cli.build_context")
def test_update_segment_flags_mismatched_read_back(mock_build):
    ctx_obj, calls = _fake_ctx_factory([
        _SEG_CURRENT, {"data": {}}, _seg("Old Name", _SEG_CURRENT["data"]["attributes"]["definition"])])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["update-segment", "SEG1", "--name", "New Name"])
    assert result.exit_code != 0
    assert "reads back" in result.output


def test_update_segment_requires_a_change():
    result = CliRunner().invoke(main, ["update-segment", "SEG1"])
    assert result.exit_code != 0
    assert "Nothing to change" in result.output


def test_render_shows_metric_filters_and_alltime():
    from klaviyo_cli._util import _render_definition
    definition = {"condition_groups": [{"conditions": [{
        "type": "profile-metric", "metric_id": "PO1", "measurement": "count",
        "measurement_filter": {"type": "numeric", "operator": "greater-than", "value": 0},
        "timeframe_filter": {"type": "date", "operator": "alltime"},
        "metric_filters": [{"property": "Collections", "filter": {
            "type": "list", "operator": "contains-any", "value": ["A", "B", "C", "D", "E"]}}],
    }]}]}
    out = _render_definition(definition, {"PO1": {"name": "Placed Order"}}, oneline=True)
    assert out == "Placed Order >0 over all time where Collections contains-any [A, B, C, +2 more]"


@patch("klaviyo_cli.cli.build_context")
def test_update_segment_lists_added_and_removed_filter_values(mock_build):
    import json

    def seg_def(values):
        return {"condition_groups": [{"conditions": [{
            "type": "profile-metric", "metric_id": "TY5Ysg", "measurement": "count",
            "measurement_filter": {"type": "numeric", "operator": "greater-than", "value": 0},
            "timeframe_filter": {"type": "date", "operator": "alltime"},
            "metric_filters": [{"property": "Collections",
                                "filter": {"type": "list", "operator": "contains-any", "value": values}}],
        }]}]}

    ctx_obj, calls = _fake_ctx_factory([_seg("S", seg_def(["A", "B"])), _METRICS_PAGE])
    mock_build.return_value = ctx_obj
    result = CliRunner().invoke(main, ["update-segment", "SEG1", "--body",
                                       json.dumps(seg_def(["B", "C"])), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "+ Collections: C" in result.output
    assert "- Collections: A" in result.output

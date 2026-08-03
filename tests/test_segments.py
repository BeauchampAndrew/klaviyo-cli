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

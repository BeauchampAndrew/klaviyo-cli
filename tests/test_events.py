from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main


def _fake_ctx_factory(pages):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body))
        return pages.pop(0)

    return {"call": call, "label": "test-account"}, calls


@patch("klaviyo_cli.cli.build_context")
def test_push_event_builds_metric_by_name_envelope(mock_build):
    # Events API returns 202 with an empty body.
    ctx_obj, calls = _fake_ctx_factory([{"_raw": ""}])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "push-event",
        "--email", "ann@example.com",
        "--metric", "Placed Order",
        "--time", "2026-07-01T12:00:00+00:00",
        "--value", "42.5",
        "--properties", '{"OrderId": "1001"}',
        "--profile-attrs", '{"first_name": "Ann"}',
    ])

    assert result.exit_code == 0, result.output
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/api/events/"
    attrs = body["data"]["attributes"]
    assert body["data"]["type"] == "event"
    assert attrs["metric"]["data"]["attributes"]["name"] == "Placed Order"
    assert attrs["profile"]["data"]["attributes"]["email"] == "ann@example.com"
    assert attrs["profile"]["data"]["attributes"]["first_name"] == "Ann"
    assert attrs["properties"] == {"OrderId": "1001"}
    assert attrs["time"] == "2026-07-01T12:00:00+00:00"
    assert attrs["value"] == 42.5
    assert "Event accepted" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_push_event_omits_optional_fields(mock_build):
    ctx_obj, calls = _fake_ctx_factory([{"_raw": ""}])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "push-event", "--email", "ann@example.com", "--metric", "Did Thing",
    ])

    assert result.exit_code == 0, result.output
    attrs = calls[0][2]["data"]["attributes"]
    assert "time" not in attrs
    assert "value" not in attrs
    assert attrs["properties"] == {}


@patch("klaviyo_cli.cli.build_context")
def test_push_event_rejects_bad_properties_json_without_calling(mock_build):
    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "push-event", "--email", "a@x.com", "--metric", "M",
        "--properties", "{not json",
    ])

    assert result.exit_code != 0
    assert "--properties" in result.output
    assert len(calls) == 0


def _event(eid, dt, pid):
    return {
        "id": eid,
        "attributes": {"datetime": dt},
        "relationships": {"profile": {"data": {"type": "profile", "id": pid}}},
    }


@patch("klaviyo_cli.cli.build_context")
def test_events_lists_with_profiles(mock_build):
    resp = {
        "data": [_event("E1", "2026-07-02T10:00:00+00:00", "P1"),
                 _event("E2", "2026-07-01T09:00:00+00:00", "P2")],
        "included": [
            {"type": "profile", "id": "P1",
             "attributes": {"email": "ann@example.com", "first_name": "Ann",
                            "last_name": "Example"}},
            {"type": "profile", "id": "P2",
             "attributes": {"email": "bob@example.com"}},
        ],
    }
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["events", "--metric", "MET1", "--limit", "5"])

    assert result.exit_code == 0, result.output
    path = calls[0][1]
    # equals(metric_id,"MET1") must be url-quoted.
    assert "filter=equals%28metric_id%2C%22MET1%22%29" in path
    assert "sort=-datetime" in path
    assert "include=profile" in path
    assert "page[size]=5" in path
    assert "ann@example.com" in result.output
    assert "Ann Example" in result.output
    assert "bob@example.com" in result.output
    assert "2026-07-02T10:00:00" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_events_combines_profile_filter_with_and(mock_build):
    resp = {"data": [], "included": []}
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["events", "--metric", "MET1", "--profile-id", "P1"]
    )

    assert result.exit_code == 0, result.output
    path = calls[0][1]
    assert ("filter=and%28equals%28metric_id%2C%22MET1%22%29%2C"
            "equals%28profile_id%2C%22P1%22%29%29") in path
    assert "page[size]=10" in path


@patch("klaviyo_cli.cli.build_context")
def test_events_window_filter_and_properties(mock_build):
    """--since/--until join into and(...) with unquoted datetimes; --properties prints payloads."""
    page = {"data": [{
        "id": "E1",
        "attributes": {"datetime": "2026-07-08T10:00:00+00:00",
                       "event_properties": {"Collections": ["Yoder Smokers"]}},
        "relationships": {"profile": {"data": {"id": "P1"}}},
    }], "included": [{"type": "profile", "id": "P1",
                      "attributes": {"email": "ann@example.com"}}],
        "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "events", "--metric", "RGkS29",
        "--since", "2026-07-06", "--until", "2026-07-13",
        "--properties", "--limit", "5",
    ])

    assert result.exit_code == 0, result.output
    from urllib.parse import unquote
    path = unquote(calls[0][1])
    assert ('and(equals(metric_id,"RGkS29"),'
            "greater-or-equal(datetime,2026-07-06),"
            "less-than(datetime,2026-07-13))") in path
    assert "ann@example.com" in result.output
    assert "Yoder Smokers" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_events_paginates_up_to_limit(mock_build):
    def ev(i):
        return {"id": f"E{i}",
                "attributes": {"datetime": f"2026-07-0{i}T00:00:00+00:00"},
                "relationships": {"profile": {"data": {"id": f"P{i}"}}}}
    page1 = {"data": [ev(1), ev(2)], "included": [],
             "links": {"next": "https://a.klaviyo.com/api/events/?page%5Bcursor%5D=NEXT"}}
    page2 = {"data": [ev(3)], "included": [], "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page1, page2])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["events", "--metric", "M1", "--limit", "3"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert "E3" not in result.output or "3 shown" in result.output


# ---------------------------------------------------------------------------
# export-events
# ---------------------------------------------------------------------------


def _export_event(i):
    return {"id": f"E{i}", "type": "event",
            "attributes": {"datetime": f"2026-08-1{i}T00:00:00+00:00",
                           "event_properties": {"$value": i * 10.0}}}


@patch("klaviyo_cli.cli.build_context")
def test_export_events_streams_ndjson_across_all_pages(mock_build):
    import json

    page1 = {"data": [_export_event(1), _export_event(2)],
             "links": {"next": "https://a.klaviyo.com/api/events/?page%5Bcursor%5D=NEXT"}}
    page2 = {"data": [_export_event(3)], "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page1, page2])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "export-events", "--metric", "XNPwvq",
        "--since", "2026-08-11", "--until", "2026-08-14",
    ])

    assert result.exit_code == 0, result.output
    # Exhaustive: follows the cursor with no limit.
    assert len(calls) == 2
    path = calls[0][1]
    assert "equals%28metric_id%2C%22XNPwvq%22%29" in path
    assert "greater-or-equal%28datetime%2C2026-08-11%29" in path
    assert "less-than%28datetime%2C2026-08-14%29" in path
    assert "page[size]=200" in path
    assert "sort=datetime" in path  # ascending: stable for exports
    # Sparse fields by default: events are huge; only pull what reports need.
    assert "fields[event]=datetime,event_properties" in path
    # Second call follows the next link with the origin stripped.
    assert calls[1][1].startswith("/api/events/?page")
    # stdout is pure NDJSON: one parseable object per line, in order.
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(lines) == 3
    parsed = [json.loads(l) for l in lines]
    assert [p["id"] for p in parsed] == ["E1", "E2", "E3"]
    assert parsed[0]["attributes"]["event_properties"] == {"$value": 10.0}


@patch("klaviyo_cli.cli.build_context")
def test_export_events_writes_out_file(mock_build):
    import json

    page = {"data": [_export_event(1)], "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, [
            "export-events", "--metric", "M1", "--out", "events.ndjson",
        ])
        assert result.exit_code == 0, result.output
        with open("events.ndjson") as f:
            lines = [json.loads(l) for l in f if l.strip()]
    assert [l["id"] for l in lines] == ["E1"]
    assert result.stdout == ""  # NDJSON went to the file, not stdout


@patch("klaviyo_cli.cli.build_context")
def test_export_events_max_pages_guard_warns_on_truncation(mock_build):
    endless = {"data": [_export_event(1)],
               "links": {"next": "https://a.klaviyo.com/api/events/?page%5Bcursor%5D=X"}}
    ctx_obj, calls = _fake_ctx_factory([dict(endless), dict(endless), dict(endless)])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "export-events", "--metric", "M1", "--max-pages", "2",
    ])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    # Never truncate silently.
    assert "truncated" in result.stderr.lower()


@patch("klaviyo_cli.cli.build_context")
def test_export_events_fields_all_drops_sparse_fieldset(mock_build):
    page = {"data": [_export_event(1)], "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, [
        "export-events", "--metric", "M1", "--fields", "all",
    ])

    assert result.exit_code == 0, result.output
    assert "fields[event]" not in calls[0][1]

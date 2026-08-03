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


@patch("klaviyo_cli.cli.build_context")
def test_flows_sort_and_search(mock_build):
    """--sort adds a server-side sort param; --search filters client-side."""
    page = {
        "data": [_flow("AAA111", "Welcome Flow"), _flow("BBB222", "Add To Cart")],
        "links": {"next": None},
    }
    ctx_obj, calls = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["flows", "--sort", "created", "--search", "welcome"])

    assert result.exit_code == 0, result.output
    assert "&sort=-created" in calls[0][1]
    assert "AAA111" in result.output
    assert "BBB222" not in result.output
    assert "1 of 2" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_flows_sort_name_ascending(mock_build):
    page = {"data": [], "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["flows", "--sort", "name"])

    assert result.exit_code == 0, result.output
    assert "&sort=name" in calls[0][1]


@patch("klaviyo_cli.cli.build_context")
def test_get_flow_basic(mock_build):
    """get-flow without --definition is a single GET with no additional-fields."""
    resp = {
        "data": {
            "id": "FLOW1",
            "attributes": {
                "name": "Welcome Flow",
                "status": "live",
                "trigger_type": "List",
                "created": "2025-01-01T00:00:00+00:00",
                "updated": "2026-05-01T00:00:00+00:00",
            },
        }
    }
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["get-flow", "FLOW1"])

    assert result.exit_code == 0, result.output
    assert calls[0][1] == "/api/flows/FLOW1/"
    assert "Welcome Flow" in result.output
    assert "live" in result.output
    assert "List" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_flow_definition_prints_chain_and_webhook_url(mock_build):
    resp = {
        "data": {
            "id": "FLOW1",
            "attributes": {
                "name": "Sync Flow",
                "status": "draft",
                "trigger_type": "Metric",
                "created": "2025-01-01T00:00:00+00:00",
                "updated": "2026-05-01T00:00:00+00:00",
                "definition": {
                    "triggers": [{"type": "metric", "id": "MET1"}],
                    "actions": [
                        {"id": "A1", "type": "send-email", "links": {"next": "A2"}},
                        {"id": "A2", "type": "send-webhook",
                         "data": {"url": "https://example.com/hook"},
                         "links": {"next": None}},
                    ],
                    "entry_action_id": "A1",
                    "reentry_criteria": {"duration": 30, "unit": "day"},
                },
            },
        }
    }
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["get-flow", "FLOW1", "--definition"])

    assert result.exit_code == 0, result.output
    assert "additional-fields[flow]=definition" in calls[0][1]
    assert "metric" in result.output and "MET1" in result.output
    assert "send-email" in result.output
    assert "send-webhook" in result.output
    assert "https://example.com/hook" in result.output
    assert "every 30 day(s)" in result.output


_FLOW_DEF = {
    "triggers": [{"type": "metric", "id": "MET1"}],
    "actions": [{"temporary_id": "A1", "type": "send-email", "links": {"next": None}}],
    "entry_action_id": "A1",
}

_EXISTING_FLOWS = {
    "data": [{"id": "F1", "attributes": {"name": "Welcome Flow"}}],
    "links": {"next": None},
}


@patch("klaviyo_cli.cli.build_context")
def test_create_flow_posts_envelope(mock_build):
    import json

    created = {"data": {"id": "NEW1", "attributes": {"name": "My New Flow"}}}
    ctx_obj, calls = _fake_ctx_factory([_EXISTING_FLOWS, created])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["create-flow", "--name", "My New Flow", "--body", json.dumps(_FLOW_DEF)]
    )

    assert result.exit_code == 0, result.output
    # Read (dedup names) then POST.
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert calls[1][1] == "/api/flows/"
    body = calls[1][2]
    assert body["data"]["type"] == "flow"
    assert body["data"]["attributes"]["name"] == "My New Flow"
    assert body["data"]["attributes"]["definition"] == _FLOW_DEF
    assert "NEW1" in result.output
    assert "Review in Klaviyo UI before setting live" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_create_flow_rejects_action_ids_before_any_call(mock_build):
    """A definition whose actions use "id" must error with zero API calls."""
    import json

    bad_def = {
        "triggers": [{"type": "metric", "id": "MET1"}],
        "actions": [{"id": "A1", "type": "send-email", "links": {"next": None}}],
        "entry_action_id": "A1",
    }
    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["create-flow", "--name", "My New Flow", "--body", json.dumps(bad_def)]
    )

    assert result.exit_code != 0
    assert "temporary_id" in result.output
    assert len(calls) == 0


@patch("klaviyo_cli.cli.build_context")
def test_create_flow_fix_ids_renames_actions(mock_build):
    import json

    bad_def = {
        "triggers": [{"type": "metric", "id": "MET1"}],
        "actions": [{"id": "A1", "type": "send-email", "links": {"next": None}}],
        "entry_action_id": "A1",
    }
    created = {"data": {"id": "NEW1", "attributes": {"name": "My New Flow"}}}
    ctx_obj, calls = _fake_ctx_factory([_EXISTING_FLOWS, created])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main,
        ["create-flow", "--name", "My New Flow", "--body", json.dumps(bad_def), "--fix-ids"],
    )

    assert result.exit_code == 0, result.output
    posted = calls[1][2]["data"]["attributes"]["definition"]
    assert posted["actions"][0]["temporary_id"] == "A1"
    assert "id" not in posted["actions"][0]
    # Trigger ids are legitimate and must NOT be renamed.
    assert posted["triggers"][0]["id"] == "MET1"


@patch("klaviyo_cli.cli.build_context")
def test_create_flow_blocks_duplicate_name(mock_build):
    """Duplicate name aborts after the read; the POST must not happen."""
    import json

    ctx_obj, calls = _fake_ctx_factory([_EXISTING_FLOWS])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["create-flow", "--name", "Welcome Flow", "--body", json.dumps(_FLOW_DEF)]
    )

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert "F1" in result.output
    assert len(calls) == 1
    assert calls[0][0] == "GET"


@patch("klaviyo_cli.cli.build_context")
def test_create_flow_bare_definition_requires_name(mock_build):
    import json

    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["create-flow", "--body", json.dumps(_FLOW_DEF)])

    assert result.exit_code != 0
    assert "--name" in result.output
    assert len(calls) == 0


@patch("klaviyo_cli.cli.build_context")
def test_flow_performance_total_row_formats(mock_build):
    """Regression: the TOTAL row used '::<50' which rendered a fill of colons."""
    metrics_resp = {"data": [{"id": "M1", "attributes": {
        "name": "Placed Order", "integration": {"name": "Shopify"}}}]}
    names_page = {"data": [{"id": "F1", "attributes": {"name": "Welcome Flow"}}],
                  "links": {"next": None}}
    report = {"data": {"attributes": {"results": [{
        "groupings": {"flow_id": "F1"},
        "statistics": {"conversion_value": 100.0, "delivered": 10,
                       "opens_unique": 5, "clicks_unique": 2,
                       "unsubscribes": 0, "spam_complaints": 0},
    }]}}}
    ctx_obj, calls = _fake_ctx_factory([metrics_resp, names_page, report])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["flow-performance"])

    assert result.exit_code == 0, result.output
    assert "TOTAL" in result.output
    assert ":::" not in result.output

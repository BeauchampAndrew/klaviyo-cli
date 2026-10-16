from unittest.mock import patch

import pytest
from click.testing import CliRunner

from klaviyo_cli.cli import main
from klaviyo_cli.transport import AuthError, confirmed_delete, ensure_delete_allowed


def _ctx(flow_def=None, audiences=None):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path))
        if method == "DELETE":
            ensure_delete_allowed(path)
            return {"_raw": ""}
        if path.startswith("/api/segments/SEG1/"):
            return {"data": {"attributes": {"name": "Old Seg", "profile_count": 12}}}
        if path.startswith("/api/flows/?"):
            return {"data": [{"id": "FL1", "attributes": {"name": "Push", "status": "live"}}]}
        if path.startswith("/api/flows/FL1/"):
            return {"data": {"attributes": {"definition": flow_def or {}}}}
        if path.startswith("/api/campaigns/"):
            return {"data": [{"id": "C1", "attributes": {"name": "Promo", "audiences": audiences or {}}}]}
        raise AssertionError(path)

    return {"call": call, "label": "t", "json": False}, calls


def test_raw_delete_of_segment_still_blocked():
    with pytest.raises(AuthError, match="klaviyo delete"):
        ensure_delete_allowed("/api/segments/SEG1/")


def test_confirmed_delete_only_unlocks_that_path():
    with confirmed_delete("/api/segments/SEG1/"):
        ensure_delete_allowed("/api/segments/SEG1/")
        with pytest.raises(AuthError):
            ensure_delete_allowed("/api/segments/OTHER/")
    with pytest.raises(AuthError):
        ensure_delete_allowed("/api/segments/SEG1/")


@patch("klaviyo_cli.cli.build_context")
def test_delete_without_confirm_does_nothing(mock_build):
    ctx, calls = _ctx()
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "segment", "SEG1"])
    assert result.exit_code != 0
    assert "--confirm SEG1" in result.output
    assert not any(m == "DELETE" for m, _ in calls)


@patch("klaviyo_cli.cli.build_context")
def test_delete_wrong_confirm_does_nothing(mock_build):
    ctx, calls = _ctx()
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "segment", "SEG1", "--confirm", "SEG2"])
    assert result.exit_code != 0
    assert not any(m == "DELETE" for m, _ in calls)


@patch("klaviyo_cli.cli.build_context")
def test_delete_confirmed_runs_and_warns_about_flow(mock_build):
    ctx, calls = _ctx(flow_def={"triggers": [{"type": "segment", "id": "SEG1"}]})
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["delete", "segment", "SEG1", "--confirm", "SEG1"])
    assert result.exit_code == 0, result.output
    assert "flow [FL1] Push (live)" in result.output
    assert ("DELETE", "/api/segments/SEG1/") in calls

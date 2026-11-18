from unittest.mock import patch

import pytest
from click.testing import CliRunner

from klaviyo_cli.cli import main
from klaviyo_cli.transport import AuthError, confirmed_delete, ensure_delete_allowed

PATH = "/api/lists/L1/relationships/profiles/"


def _ctx(members=("P1", "P2")):
    """Fake API: a@x.com (P1) and b@x.com (P2) exist; members start on list L1;
    a successful DELETE takes the posted profiles off it."""
    on_list = set(members)
    calls = []
    profiles = {"a@x.com": "P1", "b@x.com": "P2", "c@x.com": "P3"}

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body))
        if method == "DELETE":
            ensure_delete_allowed(path)
            for item in body["data"]:
                on_list.discard(item["id"])
            return {"_raw": ""}
        if path.startswith("/api/lists/L1/"):
            return {"data": {"attributes": {"name": "Newsletter"}}}
        if path.startswith("/api/profiles/?filter="):
            for email, pid in profiles.items():
                if email in path.replace("%40", "@"):
                    return {"data": [{"id": pid, "attributes": {"email": email, "first_name": pid}}]}
            return {"data": []}
        if path.startswith("/api/profiles/") and "/lists/" in path:
            pid = path.split("/")[3]
            return {"data": [{"id": "L1"}] if pid in on_list else [{"id": "OTHER"}]}
        raise AssertionError(path)

    return {"call": call, "label": "t", "json": False}, calls, on_list


def test_raw_delete_of_list_members_is_blocked_without_the_command():
    with pytest.raises(AuthError):
        ensure_delete_allowed(PATH)


def test_confirmed_path_only_unlocks_that_list():
    with confirmed_delete(PATH):
        ensure_delete_allowed(PATH)
        with pytest.raises(AuthError):
            ensure_delete_allowed("/api/lists/OTHER/relationships/profiles/")


@patch("klaviyo_cli.cli.build_context")
def test_without_yes_in_a_script_nothing_is_removed(mock_build):
    ctx, calls, on_list = _ctx()
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["list-remove", "L1", "--email", "a@x.com"])
    assert result.exit_code != 0
    assert "--yes" in result.output
    assert "profiles, history, consent and other lists are kept" in result.output
    assert not any(m == "DELETE" for m, _, _ in calls)
    assert on_list == {"P1", "P2"}


@patch("klaviyo_cli.cli.build_context")
def test_removes_only_members_and_skips_the_rest(mock_build):
    ctx, calls, on_list = _ctx(members=("P1",))
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["list-remove", "L1", "--email", "A@x.com",
                                       "--email", "b@x.com", "--email", "nobody@x.com", "--yes"])
    assert result.exit_code == 0, result.output
    deletes = [b for m, _, b in calls if m == "DELETE"]
    assert deletes == [{"data": [{"type": "profile", "id": "P1"}]}]
    assert "skip b@x.com: not on this list" in result.output
    assert "skip nobody@x.com: no profile" in result.output
    assert "Removed 1 from [L1] Newsletter." in result.output
    assert on_list == set()


@patch("klaviyo_cli.cli.build_context")
def test_nobody_to_remove_sends_nothing(mock_build):
    ctx, calls, _ = _ctx(members=())
    mock_build.return_value = ctx
    result = CliRunner().invoke(main, ["list-remove", "L1", "--email", "a@x.com", "--yes"])
    assert result.exit_code != 0
    assert "Nobody to remove" in result.output
    assert not any(m == "DELETE" for m, _, _ in calls)

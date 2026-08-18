from unittest.mock import patch

from click.testing import CliRunner

from klaviyo_cli.cli import main


def _fake_ctx_factory(pages):
    calls = []

    def call(method, path, body=None, revision=None):
        calls.append((method, path, body))
        return pages.pop(0)

    return {"call": call, "label": "test-account"}, calls


_PROFILE_ATTRS = {
    "email": "ann@example.com",
    "first_name": "Ann",
    "last_name": "Example",
    "created": "2025-01-01T00:00:00+00:00",
    "last_event_date": "2026-07-01T00:00:00+00:00",
}


@patch("klaviyo_cli.cli.build_context")
def test_get_profile_by_email_uses_quoted_filter(mock_build):
    resp = {"data": [{"id": "PROF1", "attributes": dict(_PROFILE_ATTRS)}]}
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["get-profile", "ann@example.com"])

    assert result.exit_code == 0, result.output
    path = calls[0][1]
    assert path.startswith("/api/profiles/?filter=")
    # equals(email,"ann@example.com") must be url-quoted.
    assert "equals%28email%2C%22ann%40example.com%22%29" in path
    assert "PROF1" in result.output
    assert "ann@example.com" in result.output
    assert "Ann Example" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_profile_by_id(mock_build):
    resp = {"data": {"id": "PROF1", "attributes": dict(_PROFILE_ATTRS)}}
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["get-profile", "PROF1"])

    assert result.exit_code == 0, result.output
    assert calls[0][1] == "/api/profiles/PROF1/"
    assert "ann@example.com" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_profile_subscriptions_shows_suppressions(mock_build):
    attrs = dict(_PROFILE_ATTRS)
    attrs["subscriptions"] = {
        "email": {
            "marketing": {
                "can_receive_email_marketing": False,
                "consent": "NEVER_SUBSCRIBED",
                "suppression": [
                    {"reason": "USER_SUPPRESSED", "timestamp": "2026-06-01T00:00:00+00:00"}
                ],
            }
        }
    }
    resp = {"data": {"id": "PROF1", "attributes": attrs}}
    ctx_obj, calls = _fake_ctx_factory([resp])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["get-profile", "PROF1", "--subscriptions"])

    assert result.exit_code == 0, result.output
    assert "additional-fields[profile]=subscriptions" in calls[0][1]
    assert "Can receive: False" in result.output
    assert "NEVER_SUBSCRIBED" in result.output
    assert "USER_SUPPRESSED" in result.output
    assert "2026-06-01" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_get_profile_email_not_found(mock_build):
    ctx_obj, calls = _fake_ctx_factory([{"data": []}])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["get-profile", "nobody@example.com"])

    assert result.exit_code != 0
    assert "No profile found" in result.output


_JOB_RESP = {"data": {"id": "JOB1", "attributes": {"status": "queued"}}}


@patch("klaviyo_cli.cli.build_context")
def test_suppress_with_yes_posts_bulk_job(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_JOB_RESP])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["suppress", "--emails", "a@x.com,b@y.com", "--yes"]
    )

    assert result.exit_code == 0, result.output
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/api/profile-suppression-bulk-create-jobs/"
    assert body["data"]["type"] == "profile-suppression-bulk-create-job"
    profiles = body["data"]["attributes"]["profiles"]["data"]
    assert [p["attributes"]["email"] for p in profiles] == ["a@x.com", "b@y.com"]
    assert all(p["type"] == "profile" for p in profiles)
    assert "2 email(s)" in result.output
    assert "JOB1" in result.output
    assert "queued" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_suppress_without_yes_noninteractive_makes_no_calls(mock_build):
    """Without --yes and no interactive confirmation, nothing is POSTed."""
    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["suppress", "--emails", "a@x.com"])

    assert result.exit_code != 0
    assert len(calls) == 0


@patch("klaviyo_cli.cli.build_context")
def test_suppress_confirmation_declined_makes_no_calls(mock_build):
    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["suppress", "--emails", "a@x.com"], input="n\n")

    assert result.exit_code != 0
    assert len(calls) == 0


@patch("klaviyo_cli.cli.build_context")
def test_suppress_requires_emails_or_file(mock_build):
    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["suppress", "--yes"])

    assert result.exit_code != 0
    assert "--emails" in result.output
    assert len(calls) == 0


@patch("klaviyo_cli.cli.build_context")
def test_suppress_reads_file(mock_build, tmp_path):
    emails_file = tmp_path / "emails.txt"
    emails_file.write_text("a@x.com\n\nb@y.com\n")
    ctx_obj, calls = _fake_ctx_factory([_JOB_RESP])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["suppress", "--file", str(emails_file), "--yes"]
    )

    assert result.exit_code == 0, result.output
    profiles = calls[0][2]["data"]["attributes"]["profiles"]["data"]
    assert [p["attributes"]["email"] for p in profiles] == ["a@x.com", "b@y.com"]


@patch("klaviyo_cli.cli.build_context")
def test_unsuppress_posts_delete_job_without_confirmation(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_JOB_RESP])
    mock_build.return_value = ctx_obj

    # No --yes and no input: unsuppress must not prompt.
    result = CliRunner().invoke(main, ["unsuppress", "--emails", "a@x.com"])

    assert result.exit_code == 0, result.output
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/api/profile-suppression-bulk-delete-jobs/"
    assert body["data"]["type"] == "profile-suppression-bulk-delete-job"
    assert body["data"]["attributes"]["profiles"]["data"][0]["attributes"]["email"] == "a@x.com"


def _job(jid, status="complete"):
    return {"id": jid, "attributes": {
        "status": status, "created": "2026-07-01T00:00:00+00:00",
        "total_count": 10, "completed_count": 9, "skipped_count": 1}}


@patch("klaviyo_cli.cli.build_context")
def test_suppression_jobs_both_directions(mock_build):
    create_page = {"data": [_job("CJOB1")]}
    delete_page = {"data": [_job("DJOB1", "processing")]}
    ctx_obj, calls = _fake_ctx_factory([create_page, delete_page])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["suppression-jobs"])

    assert result.exit_code == 0, result.output
    assert calls[0][1] == "/api/profile-suppression-bulk-create-jobs/"
    assert calls[1][1] == "/api/profile-suppression-bulk-delete-jobs/"
    assert "CJOB1" in result.output and "suppress" in result.output
    assert "DJOB1" in result.output and "unsuppress" in result.output
    assert "total: 10" in result.output
    assert "completed: 9" in result.output
    assert "skipped: 1" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_suppression_jobs_create_only(mock_build):
    ctx_obj, calls = _fake_ctx_factory([{"data": [_job("CJOB1")]}])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["suppression-jobs", "--type", "create"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][1] == "/api/profile-suppression-bulk-create-jobs/"


def _member(pid, email):
    return {"id": pid, "attributes": {
        "email": email, "first_name": "Ann", "last_name": "Example",
        "joined_group_at": "2026-06-15T00:00:00+00:00"}}


@patch("klaviyo_cli.cli.build_context")
def test_segment_members_default_limit(mock_build):
    page = {"data": [_member("P1", "a@x.com"), _member("P2", "b@y.com")],
            "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["segment-members", "SEG1"])

    assert result.exit_code == 0, result.output
    assert calls[0][1] == "/api/segments/SEG1/profiles/?page[size]=20"
    assert "a@x.com" in result.output
    assert "b@y.com" in result.output
    assert "2026-06-15" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_segment_members_paginates_and_truncates_over_100(mock_build):
    page1 = {
        "data": [_member(f"P{i}", f"u{i}@x.com") for i in range(100)],
        "links": {"next": "https://a.klaviyo.com/api/segments/SEG1/profiles/?page%5Bcursor%5D=NEXT"},
    }
    page2 = {"data": [_member(f"P{i}", f"u{i}@x.com") for i in range(100, 200)],
             "links": {"next": None}}
    ctx_obj, calls = _fake_ctx_factory([page1, page2])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["segment-members", "SEG1", "--limit", "150"])

    assert result.exit_code == 0, result.output
    # page[size] is capped at 100, and pagination fills up to the limit.
    assert "page[size]=100" in calls[0][1]
    assert len(calls) == 2
    assert "(150 shown)" in result.output
    assert "u149@x.com" in result.output
    assert "u150@x.com" not in result.output


@patch("klaviyo_cli.cli.build_context")
def test_unsubscribe_with_yes_posts_bulk_job(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_JOB_RESP])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["unsubscribe", "--emails", "a@x.com,b@y.com", "--yes"]
    )

    assert result.exit_code == 0, result.output
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/api/profile-subscription-bulk-delete-jobs/"
    assert body["data"]["type"] == "profile-subscription-bulk-delete-job"
    assert "relationships" not in body["data"]
    profiles = body["data"]["attributes"]["profiles"]["data"]
    assert [p["attributes"]["email"] for p in profiles] == ["a@x.com", "b@y.com"]
    assert "2 email(s)" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_unsubscribe_without_yes_noninteractive_makes_no_calls(mock_build):
    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(main, ["unsubscribe", "--emails", "a@x.com"])

    assert result.exit_code != 0
    assert len(calls) == 0


@patch("klaviyo_cli.cli.build_context")
def test_unsubscribe_confirmation_declined_makes_no_calls(mock_build):
    ctx_obj, calls = _fake_ctx_factory([])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["unsubscribe", "--emails", "a@x.com"], input="n\n"
    )

    assert result.exit_code != 0
    assert len(calls) == 0


@patch("klaviyo_cli.cli.build_context")
def test_unsubscribe_batches_over_100(mock_build, tmp_path):
    emails_file = tmp_path / "emails.txt"
    emails_file.write_text("".join(f"u{i}@x.com\n" for i in range(154)))
    # 202s with empty bodies, one per batch.
    ctx_obj, calls = _fake_ctx_factory([{}, {}])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["unsubscribe", "--file", str(emails_file), "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    first = calls[0][2]["data"]["attributes"]["profiles"]["data"]
    second = calls[1][2]["data"]["attributes"]["profiles"]["data"]
    assert len(first) == 100
    assert len(second) == 54
    assert "154 email(s)" in result.output
    assert "2 jobs" in result.output
    assert "Accepted (202)" in result.output


@patch("klaviyo_cli.cli.build_context")
def test_unsubscribe_list_scoped(mock_build):
    ctx_obj, calls = _fake_ctx_factory([_JOB_RESP])
    mock_build.return_value = ctx_obj

    result = CliRunner().invoke(
        main, ["unsubscribe", "--emails", "a@x.com", "--list", "LIST1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    body = calls[0][2]
    assert body["data"]["relationships"]["list"]["data"] == {
        "type": "list", "id": "LIST1"
    }

"""SMS campaign commands: upload-sms."""

import click

from .._util import _parse_send_time, output
from ..cli import main
from ..transport import APIError, AuthError

# ---------------------------------------------------------------------------
# upload-sms
# ---------------------------------------------------------------------------


@main.command("upload-sms")
@click.option("--name", required=True, help="Campaign name (e.g. 'SMS [04-11-2026] Heavy Flow')")
@click.option("--body", "body_text", required=True, help="SMS body text, or @path/to/file.txt")
@click.option("--date", "date_val", required=True, help="Send date (MM-DD-YYYY)")
@click.option("--time", "time_val", required=True, help="Send time (e.g. '3:00 PM EDT')")
@click.option("--include", "include_ids", required=True, help="Comma-separated segment IDs to include")
@click.option("--exclude", "exclude_ids", default=None, help="Comma-separated segment IDs to exclude")
@click.pass_context
def upload_sms(ctx, name, body_text, date_val, time_val, include_ids, exclude_ids):
    """Create an SMS campaign draft in Klaviyo."""
    use_json = ctx.obj["json"]
    try:
        # Resolve body text
        if body_text.startswith("@"):
            with open(body_text[1:]) as f:
                sms_body = f.read().strip()
        else:
            sms_body = body_text

        # Parse send time
        send_datetime = _parse_send_time(date_val, time_val)

        # Parse audiences
        included = [i.strip() for i in include_ids.split(",") if i.strip()]
        excluded = (
            [i.strip() for i in exclude_ids.split(",") if i.strip()]
            if exclude_ids
            else []
        )

        payload = {
            "data": {
                "type": "campaign",
                "attributes": {
                    "name": name,
                    "audiences": {
                        "included": included,
                        "excluded": excluded,
                    },
                    "send_options": {"use_smart_sending": True},
                    "tracking_options": {
                        "add_tracking_params": False,
                        "custom_tracking_params": [],
                    },
                    "send_strategy": {
                        "method": "static",
                        "datetime": send_datetime,
                        "options": {"is_local": False},
                    },
                    "campaign-messages": {
                        "data": [
                            {
                                "type": "campaign-message",
                                "attributes": {
                                    "definition": {
                                        "channel": "sms",
                                        "content": {
                                            "body": sms_body,
                                        },
                                        "render_options": {
                                            "shorten_links": True,
                                            "add_org_prefix": True,
                                            "add_info_link": True,
                                            "add_opt_out_language": True,
                                        },
                                    }
                                },
                            }
                        ]
                    },
                },
            }
        }

        data = ctx.obj["call"]("POST", "/api/campaigns/", body=payload)

        if use_json:
            output(data, use_json=True)
        else:
            item = data.get("data", {})
            campaign_id = item.get("id", "?")
            attrs = item.get("attributes", {})
            print(f"Created SMS draft: {campaign_id}")
            print(f"  Name:      {attrs.get('name', name)}")
            print(f"  Body:      {sms_body[:80]}{'...' if len(sms_body) > 80 else ''}")
            print(f"  Send:      {send_datetime}")
            print(f"  Included:  {', '.join(included)}")
            if excluded:
                print(f"  Excluded:  {', '.join(excluded)}")
            print()
            print(f"To schedule: klaviyo schedule {campaign_id}")
            print()
            print("Note: Klaviyo UI will display the send time in UTC.")
            print("The actual send is correct. To fix the UI display,")
            print("open the campaign in the Klaviyo dashboard and re-save.")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

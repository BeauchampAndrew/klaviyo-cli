"""Metrics commands: account health, sent-campaign windows, metric catalog, form performance."""

import urllib.parse
from datetime import datetime

import click

from .._util import _resolve_date_range, _resolve_metrics, output
from ..cli import main
from ..transport import APIError, AuthError

# ---------------------------------------------------------------------------
# account-health
# ---------------------------------------------------------------------------


@main.command("account-health")
@click.pass_context
def account_health(ctx):
    """Show profiles count, lists, and metrics for a client."""
    use_json = ctx.obj["json"]
    try:
        profiles_resp = ctx.obj["call"]("GET", "/api/profiles/?page[size]=1")
        lists_resp = ctx.obj["call"]("GET", "/api/lists/?fields[list]=name,created,updated")
        metrics_resp = ctx.obj["call"]("GET", "/api/metrics/")

        if use_json:
            output(
                {
                    "profiles": profiles_resp,
                    "lists": lists_resp,
                    "metrics": metrics_resp,
                },
                use_json=True,
            )
        else:
            total = profiles_resp.get("meta", {}).get("total", "unknown")
            print(f"Account Health for {ctx.obj['label']}:")
            print()
            print(f"  Total Profiles: {total}")
            print()
            print("  Lists:")
            for item in lists_resp.get("data", []):
                attrs = item.get("attributes", {})
                updated = (attrs.get("updated") or "").split("T")[0] or "?"
                print(f"    {attrs.get('name', '?')} (updated: {updated})")
            print()
            print("  Metrics:")
            for item in metrics_resp.get("data", [])[:10]:
                attrs = item.get("attributes", {})
                integration = attrs.get("integration", {}) or {}
                integration_name = integration.get("name", "N/A")
                print(f"    {attrs.get('name', '?')} (integration: {integration_name})")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


@main.command("metrics")
@click.option("--days", default=30, show_default=True, help="Number of days to look back")
@click.option("--since", help="Start date YYYY-MM-DD (overrides --days)")
@click.option("--until", help="End date YYYY-MM-DD (defaults to today)")
@click.pass_context
def metrics(ctx, days, since, until):
    """Show sent campaigns for a client within a date window."""
    use_json = ctx.obj["json"]
    try:
        start, end, label = _resolve_date_range(days, since, until)

        # Klaviyo campaign-filter quirk: datetime values must NOT be quoted.
        filter_str = (
            "equals(messages.channel,'email'),equals(status,'Sent'),"
            f"greater-or-equal(scheduled_at,{start.strftime('%Y-%m-%dT%H:%M:%SZ')}),"
            f"less-than(scheduled_at,{end.strftime('%Y-%m-%dT%H:%M:%SZ')})"
        )
        encoded = urllib.parse.quote(filter_str)
        path = (
            f"/api/campaigns/?filter={encoded}"
            "&sort=-scheduled_at"
            "&fields[campaign]=name,status,send_time"
        )
        data = ctx.obj["call"]("GET", path)

        if use_json:
            output(data, use_json=True)
        else:
            items = data.get("data", [])
            print(f"Sent Campaigns for {ctx.obj['label']} ({label}):")
            if not items:
                print("  (none)")
                return
            for item in items:
                attrs = item.get("attributes", {})
                send_date = (attrs.get("send_time") or "").split("T")[0] or "?"
                print(f"  [{item['id']}] \"{attrs.get('name', '?')}\" — sent {send_date}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# list-metrics
# ---------------------------------------------------------------------------


@main.command("list-metrics")
@click.option("--search", "search_term", default=None, help="Filter by name (case-insensitive substring)")
@click.pass_context
def list_metrics(ctx, search_term):
    """List event metrics with their IDs and integration (ID<->name catalog)."""
    use_json = ctx.obj["json"]
    try:
        metric_map = _resolve_metrics(ctx.obj["call"])
        items = sorted(metric_map.items(), key=lambda kv: kv[1]["name"].lower())
        if search_term:
            s = search_term.lower()
            items = [(mid, info) for mid, info in items if s in info["name"].lower()]
        if use_json:
            output({"metrics": [{"id": mid, **info} for mid, info in items]}, use_json=True)
        else:
            print(f"Metrics for {ctx.obj['label']} ({len(items)} shown):")
            for mid, info in items:
                print(f"  {info['name']:<42} {mid:<10} {info['integration']}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# form-performance
# ---------------------------------------------------------------------------


@main.command("form-performance")
@click.option("--days", default=90, help="Days to look back")
@click.pass_context
def form_performance(ctx, days):
    """Show pop-up/form views, submits, and submit rates."""
    use_json = ctx.obj["json"]
    try:
        from datetime import timedelta, timezone

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        start_str = start.strftime("%Y-%m-%dT00:00:00")
        end_str = end.strftime("%Y-%m-%dT23:59:59")

        # Find the Viewed Form and submitted_form_step metric IDs
        metrics_resp = ctx.obj["call"]("GET", "/api/metrics/")
        view_id = submit_id = subscribe_id = None
        for m in metrics_resp.get("data", []):
            name = m.get("attributes", {}).get("name", "")
            integration = (m.get("attributes", {}).get("integration") or {}).get("name", "")
            if name == "Viewed Form" and integration == "Klaviyo":
                view_id = m["id"]
            elif name == "submitted_form_step" and integration == "Klaviyo":
                submit_id = m["id"]
            elif name == "Subscribed to Email Marketing" and integration == "Klaviyo":
                subscribe_id = m["id"]

        if not view_id or not submit_id:
            raise click.ClickException("Could not find form metrics (Viewed Form / submitted_form_step)")

        import time as _time

        # Pull views
        views_body = {
            "data": {
                "type": "metric-aggregate",
                "attributes": {
                    "metric_id": view_id,
                    "measurements": ["count"],
                    "interval": "month",
                    "filter": [
                        f"greater-or-equal(datetime,{start_str})",
                        f"less-than(datetime,{end_str})",
                    ],
                    "timezone": "America/Chicago",
                },
            }
        }
        views_resp = ctx.obj["call"]("POST", "/api/metric-aggregates/", body=views_body)

        _time.sleep(1)

        # Pull submits
        submits_body = {
            "data": {
                "type": "metric-aggregate",
                "attributes": {
                    "metric_id": submit_id,
                    "measurements": ["count"],
                    "interval": "month",
                    "filter": [
                        f"greater-or-equal(datetime,{start_str})",
                        f"less-than(datetime,{end_str})",
                    ],
                    "timezone": "America/Chicago",
                },
            }
        }
        submits_resp = ctx.obj["call"]("POST", "/api/metric-aggregates/", body=submits_body)

        _time.sleep(1)

        # Pull new email subscribers
        subs_body = {
            "data": {
                "type": "metric-aggregate",
                "attributes": {
                    "metric_id": subscribe_id,
                    "measurements": ["count"],
                    "interval": "month",
                    "filter": [
                        f"greater-or-equal(datetime,{start_str})",
                        f"less-than(datetime,{end_str})",
                    ],
                    "timezone": "America/Chicago",
                },
            }
        }
        subs_resp = ctx.obj["call"]("POST", "/api/metric-aggregates/", body=subs_body)

        # Parse results
        v_dates = views_resp.get("data", {}).get("attributes", {}).get("dates", [])
        v_data = views_resp.get("data", {}).get("attributes", {}).get("data", [])
        v_counts = v_data[0].get("measurements", {}).get("count", []) if v_data else []

        s_data = submits_resp.get("data", {}).get("attributes", {}).get("data", [])
        s_counts = s_data[0].get("measurements", {}).get("count", []) if s_data else []

        sub_data = subs_resp.get("data", {}).get("attributes", {}).get("data", [])
        sub_counts = sub_data[0].get("measurements", {}).get("count", []) if sub_data else []

        if use_json:
            output({"views": v_counts, "submits": s_counts, "subscribers": sub_counts, "dates": v_dates}, use_json=True)
        else:
            print(f"Form Performance for {ctx.obj['label']} (last {days} days):\n")
            print(f"  {'Month':<10} {'Views':>10} {'Submits':>10} {'Rate':>8} {'New Subs':>10}")
            print(f"  {'-'*52}")
            total_v = total_s = total_sub = 0
            for i, d in enumerate(v_dates):
                v = int(v_counts[i]) if i < len(v_counts) else 0
                s = int(s_counts[i]) if i < len(s_counts) else 0
                sub = int(sub_counts[i]) if i < len(sub_counts) else 0
                rate = (s / v * 100) if v > 0 else 0
                if v > 0:
                    print(f"  {d[:7]:<10} {v:>10,} {s:>10,} {rate:>7.2f}% {sub:>10,}")
                    total_v += v
                    total_s += s
                    total_sub += sub
            total_rate = (total_s / total_v * 100) if total_v > 0 else 0
            print(f"  {'-'*52}")
            print(f"  {'Total':<10} {total_v:>10,} {total_s:>10,} {total_rate:>7.2f}% {total_sub:>10,}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# metric-aggregate
# ---------------------------------------------------------------------------


@main.command("metric-aggregate")
@click.argument("metric_id")
@click.option("--measurements", default="count,unique", show_default=True,
              help="Comma list of: count, unique, sum_value")
@click.option("--interval", type=click.Choice(["hour", "day", "week", "month"]),
              default="week", show_default=True)
@click.option("--days", default=90, show_default=True, help="Days to look back")
@click.option("--since", help="Start date YYYY-MM-DD (overrides --days)")
@click.option("--until", help="End date YYYY-MM-DD (defaults to today)")
@click.option("--by", "by_fields", multiple=True,
              help="Group by an event dimension (repeatable; the API rejects "
                   "unsupported fields and names the valid ones)")
@click.option("--timezone", "tz", default="UTC", show_default=True,
              help="Bucket timezone, e.g. America/Chicago")
@click.pass_context
def metric_aggregate(ctx, metric_id, measurements, interval, days, since, until,
                     by_fields, tz):
    """Bucketed counts for one metric over time (POST /api/metric-aggregates/).

    The workhorse for trend questions: weekly Added to Cart uniques, daily
    Placed Order counts, etc. Metric IDs come from list-metrics.
    """
    use_json = ctx.obj["json"]
    try:
        from .._util import _resolve_date_range

        start, end, label = _resolve_date_range(days, since, until)
        meas = [m.strip() for m in measurements.split(",") if m.strip()]
        attributes = {
            "metric_id": metric_id,
            "measurements": meas,
            "interval": interval,
            "filter": [
                f"greater-or-equal(datetime,{start.strftime('%Y-%m-%dT%H:%M:%S')})",
                f"less-than(datetime,{end.strftime('%Y-%m-%dT%H:%M:%S')})",
            ],
            "timezone": tz,
        }
        if by_fields:
            attributes["by"] = list(by_fields)
        body = {"data": {"type": "metric-aggregate", "attributes": attributes}}
        data = ctx.obj["call"]("POST", "/api/metric-aggregates/", body=body)

        if use_json:
            output(data, use_json=True)
            return

        attrs = data.get("data", {}).get("attributes", {})
        dates = [d[:10] for d in attrs.get("dates", [])]
        widths = {m: max(len(m), 10) for m in meas}
        print(f"Metric {metric_id} by {interval} ({label}, tz {tz}):")
        for series in attrs.get("data", []):
            dims = series.get("dimensions") or []
            if dims:
                print(f"\n  {' / '.join(str(d) for d in dims)}")
            print("  " + "date".ljust(12)
                  + " ".join(m.rjust(widths[m]) for m in meas))
            counts = series.get("measurements", {})
            totals = dict.fromkeys(meas, 0.0)
            for i, d in enumerate(dates):
                row = []
                for m in meas:
                    vals = counts.get(m, [])
                    v = vals[i] if i < len(vals) else 0
                    v = v or 0
                    totals[m] += v
                    row.append((f"{v:,.2f}" if m == "sum_value" else f"{int(v):,}")
                               .rjust(widths[m]))
                print("  " + d.ljust(12) + " ".join(row))
            print("  " + "TOTAL".ljust(12)
                  + " ".join((f"{totals[m]:,.2f}" if m == "sum_value"
                              else f"{int(totals[m]):,}").rjust(widths[m])
                             for m in meas))
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

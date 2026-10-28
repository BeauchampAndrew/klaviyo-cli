"""billing-usage: current-period usage against plan caps (profiles, sends...).

Needs the `usage:read` scope ("Usage" in Klaviyo's API key settings). Keys made
before Klaviyo added that scope don't have it, even full-access ones; make a
new key. The API can only read usage: there's no endpoint to change a plan.
"""

from datetime import datetime

import click

from .._util import output
from ..cli import main
from ..transport import APIError, AuthError

LABELS = {"email_sends": "Email sends", "sms_credits": "SMS credits", "active_profiles": "Active profiles",
          "customer_hub_active_profiles": "Customer Hub", "k_social_active_profiles": "K:Social",
          "reviews_orders": "Reviews orders", "ai_credits": "AI credits"}
# Caps that bill the account. Other rows are add-on products (customer hub,
# social, reviews...) that report every active profile against the add-on's free
# allowance (e.g. 149,850 / 250), so they're listed but never flagged.
CORE = ("active_profiles", "email_sends", "sms_credits")
# Usage that accumulates over the period (vs. a count like active profiles), so
# it can be projected to the period end.
CUMULATIVE = ("email_sends", "sms_credits", "ai_credits")
# One big send early in the period would otherwise project to several times the cap.
MIN_ELAPSED_TO_PROJECT = 0.2
EXIT_OVER_WARN = 3  # distinct from click's usage-error exit code 2


def _pct(used, cap) -> float | None:
    return round(100 * used / cap, 1) if cap else None


def _projected(row: dict) -> float | None:
    """Percent of cap at period end if usage keeps its pace so far."""
    if row["type"] not in CUMULATIVE or not row["max"]:
        return None
    try:
        start, end, now = (datetime.fromisoformat(row[k]) for k in ("period_start", "period_end", "as_of"))
        elapsed = (now - start) / (end - start)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if elapsed < MIN_ELAPSED_TO_PROJECT:
        return None
    return round(100 * row["used"] / elapsed / row["max"], 1)


@main.command("billing-usage")
@click.option("--warn", type=float, default=85, show_default=True,
              help="Flag usage at or above this percent of the plan cap")
@click.pass_context
def billing_usage(ctx, warn):
    """Show this billing period's usage against the plan's caps.

    Exits with status 3 when a plan cap is at or over --warn percent (now or
    projected to the period end), so a scheduled check can alert on it.
    """
    try:
        rows = ctx.obj["call"]("GET", "/api/billing-usage/").get("data") or []
    except AuthError as e:
        raise click.ClickException(str(e))
    except APIError as e:
        if "usage:read" in str(e):
            raise click.ClickException(
                f"{e}. Give the account's API key 'Usage: Read Access' in Klaviyo "
                "(Settings > API keys); older keys don't have it even with full access.")
        raise click.ClickException(str(e))

    usage = []
    for r in rows:
        a = r.get("attributes") or {}
        used, cap = a.get("usage_used") or 0, a.get("usage_max")
        row = {"type": r.get("id"), "used": used, "max": cap, "unit": a.get("unit"),
               "percent": _pct(used, cap), "core": r.get("id") in CORE,
               "period_start": a.get("period_start"), "period_end": a.get("period_end"), "as_of": a.get("as_of")}
        row["projected_percent"] = _projected(row)
        worst = max(x for x in (row["percent"], row["projected_percent"], 0) if x is not None)
        row["over_warn"] = row["core"] and worst >= warn
        usage.append(row)
    usage.sort(key=lambda u: (not u["core"], CORE.index(u["type"]) if u["core"] else 0))
    flagged = [u for u in usage if u["over_warn"]]

    if ctx.obj["json"]:
        output({"warn_percent": warn, "usage": usage}, use_json=True)
    elif not usage:
        print("Klaviyo returned no usage rows for this account.")
    else:
        print(f"  {'':<18} {'used':>12}   {'cap':>10} {'':<9} {'now':>7} {'at period end':>14}")
        for i, u in enumerate(usage):
            if not u["core"] and (i == 0 or usage[i - 1]["core"]):
                print("  Add-on products (each counts all active profiles against its own tier; not flagged):")
            label = LABELS.get(u["type"], (u["type"] or "?").replace("_", " ").capitalize())
            pct = f"{u['percent']:.1f}%" if u["percent"] is not None else "no cap"
            proj = f"~{u['projected_percent']:.0f}%" if u["projected_percent"] is not None else ""
            cap = f"{u['max']:,}" if u["max"] else "-"
            flag = f"  <-- at or over {warn:g}%" if u["over_warn"] else ""
            print(f"  {label[:18]:<18} {u['used']:>12,.0f} / {cap:>10} {u['unit'] or '':<9} {pct:>7} {proj:>14}{flag}")
        end = (usage[0].get("period_end") or "")[:10]
        if end:
            print(f"  Period ends {end}.")
    if flagged:
        ctx.exit(EXIT_OVER_WARN)

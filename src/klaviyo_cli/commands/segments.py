"""Segment/list commands: list-audiences, sizes, counts, get, search, create."""

import json as json_module

import click

from .._util import _norm_name, _render_definition, _resolve_metrics, output
from ..cli import main
from ..transport import APIError, AuthError, KLAVIYO_BASE

# ---------------------------------------------------------------------------
# list-audiences
# ---------------------------------------------------------------------------


@main.command("list-audiences")
@click.pass_context
def list_audiences(ctx):
    """List all lists and segments for a client."""
    use_json = ctx.obj["json"]
    try:
        all_lists: list = []
        path = "/api/lists/?fields[list]=name,created,updated"
        while path:
            data = ctx.obj["call"]("GET", path)
            all_lists.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        all_segments: list = []
        path = "/api/segments/?fields[segment]=name,created,updated"
        while path:
            data = ctx.obj["call"]("GET", path)
            all_segments.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        if use_json:
            output(
                {
                    "lists": {"data": all_lists},
                    "segments": {"data": all_segments},
                },
                use_json=True,
            )
        else:
            print(f"Lists for {ctx.obj['label']} ({len(all_lists)} total):")
            for item in all_lists:
                attrs = item.get("attributes", {})
                updated = (attrs.get("updated") or "").split("T")[0] or "?"
                print(f"  [{item['id']}] {attrs.get('name', '?')} (updated: {updated})")

            print()
            print(f"Segments for {ctx.obj['label']} ({len(all_segments)} total):")
            for item in all_segments:
                attrs = item.get("attributes", {})
                updated = (attrs.get("updated") or "").split("T")[0] or "?"
                print(f"  [{item['id']}] {attrs.get('name', '?')} — updated {updated}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# segment-sizes
# ---------------------------------------------------------------------------


@main.command("segment-sizes")
@click.pass_context
def segment_sizes(ctx):
    """Show all segments with profile counts."""
    use_json = ctx.obj["json"]
    try:
        all_segments = []
        path = "/api/segments/?fields[segment]=name,created,updated"
        while path:
            data = ctx.obj["call"]("GET", path)
            all_segments.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            if next_link:
                path = next_link.replace(KLAVIYO_BASE, "")
            else:
                path = None

        if use_json:
            output(all_segments, use_json=True)
        else:
            print(f"Segments for {ctx.obj['label']} ({len(all_segments)} total):\n")
            print(f"  {'Segment':<60} {'ID':<10} {'Updated':>12}")
            print(f"  {'-'*85}")
            for s in sorted(all_segments, key=lambda x: x.get("attributes", {}).get("updated", ""), reverse=True):
                attrs = s.get("attributes", {})
                name = attrs.get("name", "?")[:58]
                updated = (attrs.get("updated") or "").split("T")[0]
                print(f"  {name:<60} {s['id']:<10} {updated:>12}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# list-sizes
# ---------------------------------------------------------------------------


@main.command("list-sizes")
@click.pass_context
def list_sizes(ctx):
    """Show all lists with profile counts, newest first.

    profile_count is rate limited hard (burst 1/s, steady 15/m), so accounts
    with many lists take a few minutes; progress prints as counts arrive.
    """
    import time as _time
    use_json = ctx.obj["json"]
    try:
        all_lists: list = []
        path = "/api/lists/?fields[list]=name,created,updated"
        while path:
            data = ctx.obj["call"]("GET", path)
            all_lists.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        all_lists.sort(key=lambda x: x.get("attributes", {}).get("created", ""), reverse=True)
        if not use_json:
            print(f"Lists for {ctx.obj['label']} ({len(all_lists)} total):\n")
            print(f"  {'List':<62} {'ID':<10} {'Created':>12} {'Profiles':>10}")
            print(f"  {'-'*98}")
        results = []
        for item in all_lists:
            detail = ctx.obj["call"](
                "GET", f"/api/lists/{item['id']}/?additional-fields[list]=profile_count"
            )
            attrs = detail.get("data", {}).get("attributes", {})
            count = attrs.get("profile_count")
            results.append({
                "id": item["id"],
                "name": attrs.get("name") or item.get("attributes", {}).get("name", "?"),
                "created": item.get("attributes", {}).get("created"),
                "profile_count": count,
            })
            if not use_json:
                created = (item.get("attributes", {}).get("created") or "").split("T")[0]
                shown = "?" if count is None else f"{count:,}"
                print(f"  {results[-1]['name'][:60]:<62} {item['id']:<10} {created:>12} {shown:>10}")
            _time.sleep(1)
        if use_json:
            output(results, use_json=True)
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# segment-count
# ---------------------------------------------------------------------------


@main.command("segment-count")
@click.argument("segment_id")
@click.pass_context
def segment_count(ctx, segment_id):
    """Get profile count for a single segment. Rate limited: 1/s, 15/min."""
    use_json = ctx.obj["json"]
    try:
        data = ctx.obj["call"](
            "GET",
            f"/api/segments/{segment_id}/?additional-fields[segment]=profile_count&fields[segment]=name,profile_count",
        )
        attrs = data.get("data", {}).get("attributes", {})
        name = attrs.get("name", "?")
        count = attrs.get("profile_count")

        if use_json:
            output({"id": segment_id, "name": name, "profile_count": count}, use_json=True)
        else:
            count_str = f"{count:,}" if count is not None else "?"
            print(f"{name}: {count_str} profiles")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# get-segment
# ---------------------------------------------------------------------------


@main.command("get-segment")
@click.argument("segment_id")
@click.pass_context
def get_segment(ctx, segment_id):
    """Show a segment's definition (conditions, metric IDs resolved) + count."""
    use_json = ctx.obj["json"]
    try:
        data = ctx.obj["call"](
            "GET",
            f"/api/segments/{segment_id}/?additional-fields[segment]=profile_count",
        )
        if use_json:
            output(data, use_json=True)
            return
        attrs = data.get("data", {}).get("attributes", {})
        definition = attrs.get("definition") or {}
        metric_map = _resolve_metrics(ctx.obj["call"])
        count = attrs.get("profile_count")
        count_str = f"{count:,}" if isinstance(count, int) else "?"
        print(f"Segment: {attrs.get('name', '?')}")
        print(f"  ID: {segment_id}")
        print(f"  Profiles: {count_str}")
        print("  Definition (groups AND'd; conditions within a group OR'd):")
        lines = _render_definition(definition, metric_map)
        if not lines:
            print("    (no rule conditions — manual or dynamic segment)")
        for i, line in enumerate(lines, 1):
            print(f"    {i}. {line}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# search-segments
# ---------------------------------------------------------------------------


@main.command("search-segments")
@click.argument("keyword")
@click.pass_context
def search_segments(ctx, keyword):
    """Find segments by name keyword; shows a one-line definition summary."""
    use_json = ctx.obj["json"]
    try:
        all_segments: list = []
        path = "/api/segments/"
        while path:
            data = ctx.obj["call"]("GET", path)
            all_segments.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        kw = keyword.lower()
        matches = [s for s in all_segments
                   if kw in (s.get("attributes", {}).get("name", "").lower())]
        if use_json:
            output({"data": matches}, use_json=True)
            return
        metric_map = _resolve_metrics(ctx.obj["call"]) if matches else {}
        print(f"Segments matching '{keyword}' for {ctx.obj['label']} "
              f"({len(matches)} of {len(all_segments)}):")
        for s in matches:
            attrs = s.get("attributes", {})
            print(f"  [{s['id']}] {attrs.get('name', '?')}")
            summary = _render_definition(attrs.get("definition") or {}, metric_map, oneline=True)
            if summary:
                print(f"       {summary}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------------
# create-segment (dedup-guarded)
# ---------------------------------------------------------------------------


@main.command("create-segment")
@click.option("--name", required=True, help="Segment name")
@click.option("--body", "body_arg", required=True,
              help="Definition JSON ({condition_groups:[...]}, or full data envelope), or @file.json")
@click.option("--force", is_flag=True, help="Create even if a same/similar-named segment exists")
@click.pass_context
def create_segment(ctx, name, body_arg, force):
    """Create a segment from a definition, guarding against duplicates."""
    use_json = ctx.obj["json"]
    try:
        if body_arg.startswith("@"):
            with open(body_arg[1:]) as f:
                parsed = json_module.load(f)
        else:
            parsed = json_module.loads(body_arg)

        # Accept: the definition itself, {definition: ...}, or a full {data: {attributes: {definition}}}
        if isinstance(parsed, dict) and "data" in parsed:
            definition = (parsed["data"].get("attributes") or {}).get("definition") or {}
        elif isinstance(parsed, dict) and "definition" in parsed:
            definition = parsed["definition"]
        else:
            definition = parsed
        if not isinstance(definition, dict) or "condition_groups" not in definition:
            raise click.ClickException(
                "Body must contain a 'condition_groups' definition (directly, "
                "under 'definition', or in a full data envelope)."
            )

        # Dedup guard — fetch existing names.
        existing: list = []
        path = "/api/segments/?fields[segment]=name"
        while path:
            data = ctx.obj["call"]("GET", path)
            existing.extend(data.get("data", []))
            next_link = data.get("links", {}).get("next")
            path = next_link.replace(KLAVIYO_BASE, "") if next_link else None

        target = _norm_name(name)
        exact = [s for s in existing if _norm_name(s.get("attributes", {}).get("name", "")) == target]
        if exact and not force:
            lines = "\n".join(f"  [{s['id']}] {s['attributes']['name']}" for s in exact)
            raise click.ClickException(
                f"A segment named '{name}' already exists:\n{lines}\n"
                f"Reuse it, or pass --force to create a duplicate anyway."
            )
        similar = [s for s in existing
                   if s not in exact
                   and (target in _norm_name(s.get("attributes", {}).get("name", ""))
                        or _norm_name(s.get("attributes", {}).get("name", "")) in target)]
        if similar and not force:
            click.echo("⚠ Similar-named segments exist — reuse one of these if it fits "
                       "(creating anyway):", err=True)
            for s in similar[:10]:
                click.echo(f"  [{s['id']}] {s['attributes']['name']}", err=True)

        payload = {"data": {"type": "segment",
                            "attributes": {"name": name, "definition": definition}}}
        result = ctx.obj["call"]("POST", "/api/segments/", body=payload)
        new = result.get("data", {})
        if use_json:
            output(result, use_json=True)
        else:
            print(f"Created segment [{new.get('id', '?')}] "
                  f"{new.get('attributes', {}).get('name', name)}")
    except (AuthError, APIError) as e:
        raise click.ClickException(str(e))

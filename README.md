# klaviyo-cli

A command-line interface for Klaviyo: campaigns, segments, flows, metrics, and scheduling. Built for humans and AI agents.

```bash
uvx --from klaviyo-cli klaviyo --help
```

No install needed. Or install it: `pipx install klaviyo-cli` or `uv tool install klaviyo-cli`. Requires Python 3.11+.

Built and maintained by [BS&Co](https://bsandco.us), a retention marketing agency for eCommerce brands. Read why we built it and how we made it safe to hand to AI agents: https://bsandco.us/blog-post/klaviyo-cli

> Unofficial. Not affiliated with, endorsed, or supported by Klaviyo, Inc.

## Quickstart

```bash
export KLAVIYO_API_KEY=pk_...
```

Get a private API key from Klaviyo under Settings > API Keys. Read commands need read scopes; commands that change data (patch, schedule, create, upload) need write scopes.

```bash
klaviyo list-campaigns --days 30
klaviyo account-health
```

## Using with Claude Code and AI agents

`--json` is a global flag: put it before the command name for machine-readable output on any command:

```bash
klaviyo --json list-campaigns --days 30
```

Deletes are gated. The raw `api` passthrough can only delete templates (`transport.py`, `DELETE_ALLOWED_PATHS`). Segments, lists, campaigns, and flows can only be deleted with `klaviyo delete <type> <id>`, which prints what it is about to remove (profile count, status, and for segments and lists every flow and draft/scheduled campaign still using it), then requires the ID typed back, or `--confirm <id>` in a non-interactive shell. Without that confirmation nothing is deleted. Profiles cannot be deleted through this tool. Two commands can stop mail going to real people, and both require an explicit `--yes` flag or an interactive confirmation before they run: `suppress` (reversible — `unsuppress` undoes it) and `unsubscribe` (it revokes consent, which `unsuppress` cannot undo; reversing it takes a deliberate call to Klaviyo's bulk subscribe endpoint asserting fresh consent, which this CLI intentionally does not wrap).

Drop this in your repo's `CLAUDE.md` so an agent knows the tool exists:

```markdown
## Klaviyo
Use the `klaviyo` CLI for Klaviyo data and actions (campaigns, segments,
flows, metrics). Auth via KLAVIYO_API_KEY env var. Pass --json before the
command for machine-readable output (e.g. `klaviyo --json list-campaigns`).
Run `klaviyo --help` for the full command list.
```

## Command reference

Run `klaviyo COMMAND --help` for full options on any command.

### Campaigns

Uses API revision `2026-10-15`, where a campaign is a container of messages and the subject, sender and template live on each message's content variation. The ID a campaign shows in the Klaviyo UI (and in reporting) is its **message** ID, so that's the ID every command prints and takes. A container campaign ID also works when the container holds one message. A draft's planned send date has no field at `2026-10-15`, so `patch-campaign --date`, `upload-sms` and `schedule` (and the planned date `get-campaign` shows) use revision `2026-07-15`.

| Command | Description |
|---|---|
| `list-campaigns` | List/filter campaigns by status, channel, and date (beyond name search) |
| `search-campaigns` | Search campaigns by name |
| `get-campaign` | Show details for a specific campaign, incl. subject/preview and planned send time; `--names` resolves audience IDs |
| `get-creative` | Dump a campaign's creative (subject + text/HTML) via its template |
| `list-drafts` | List draft email campaigns for a client |
| `patch-campaign` | Update campaign send time and/or audiences |
| `patch-message` | Change subject, preview text or sender; re-reads the campaign and fails if the change didn't stick or touched another field |
| `clone-campaign` | Duplicate a campaign into a new draft and print what it inherited (subject, from address, audiences) plus the new campaign and template IDs |
| `set-campaign-html` | Replace a campaign's HTML from a file and verify the live creative (`--expect`/`--forbid` text checks) |
| `schedule` | Schedule a campaign for sending |
| `campaign-performance` | Show campaign revenue and engagement metrics |
| `metrics` | Show sent campaigns for a client within a date window |

### Segments
| Command | Description |
|---|---|
| `list-audiences` | List all lists and segments for a client |
| `search-segments` | Find segments by name keyword; shows a one-line definition summary |
| `get-segment` | Show a segment's definition (conditions, metric IDs resolved) + count |
| `segment-count` | Get profile count for a single segment (rate limited: 1/s, 15/min) |
| `segment-sizes` | Show all segments with profile counts |
| `list-sizes` | Show all lists with profile counts, newest first |
| `create-segment` | Create a segment from a definition, guarding against duplicates |

### Flows
| Command | Description |
|---|---|
| `flows` | List all flows for a client, with optional sort and name search |
| `get-flow` | Show a flow's basics in one call; `--definition` renders the branch tree (splits, messages, delays) |
| `flow-detail` | Show full flow structure: trigger, filters, emails with subjects, delays, splits |
| `flow-performance` | Show flow revenue and engagement metrics |
| `flow-series` | Flow performance over time (daily/weekly/monthly buckets, per flow or per message) |
| `flow-actions` | List a flow's actions with status and created/updated timestamps (audit view) |
| `create-flow` | Create a flow (in draft) from a definition, guarding against duplicates |

### Profiles
| Command | Description |
|---|---|
| `get-profile` | Look up a profile by email or ID; `--subscriptions` adds consent state and suppressions |
| `segment-members` | List profiles in a segment: email, name, and when they joined |
| `suppress` | Suppress profiles from email marketing in bulk (requires `--yes` or confirmation) |
| `unsuppress` | Remove manual suppressions in bulk (never resubscribes anyone) |
| `unsubscribe` | Set email consent to UNSUBSCRIBED in bulk — `unsuppress` can't undo it; `--list` scopes to one list (requires `--yes` or confirmation) |
| `suppression-jobs` | List bulk suppression jobs (suppress + unsuppress) with status and counts |

### Events
| Command | Description |
|---|---|
| `push-event` | Push a custom event to a profile by email (creates the profile if needed) |
| `events` | List recent events for a metric ID; `--since/--until` windows, `--properties` payloads; warns when `--limit` cuts the window |
| `export-events` | Bulk-export ALL events for a metric in a window as NDJSON (exhaustive pagination; `--out` file, `--fields`, `--max-pages` guard) |

### Metrics
| Command | Description |
|---|---|
| `account-health` | Show profiles count, lists, and metrics for a client |
| `list-metrics` | List event metrics with their IDs and integration (ID<->name catalog) |
| `metric-aggregate` | Bucketed counts for one metric over time (hour/day/week/month, optional group-by) |
| `forms` | List sign-up forms with live/draft status and dates, newest first (`--status`, `--versions`) |
| `form-performance` | Show pop-up/form views, submits, and submit rates. `--form <id>` and/or `--by-version` switch to a per-form report split by version (A/B variations, or before/after each publish); `--since`/`--until` for exact windows |

### Forms
Uses Forms API revision `2026-10-15` (needs `forms:read`, plus `forms:write` to edit). The API only edits **draft** versions: live versions are echoed back unchanged, versions can't be added to or removed from an existing form, and every edit stays in draft until published. Forms with no display triggers have to be handled in the form editor.

A/B tests: `ab-test-form <form_id> --name NAME [--variant A --variant B]` creates a new draft form with one A/B variation per `--variant`, each a copy of the source's live version; edit each with `form-replace ... --version <id>`. Klaviyo adds one extra non-test version named after the form. The API can't publish a form with a draft A/B test (Klaviyo's rule), so start the test in the form editor. Read results with `form-performance --form <id> --by-version`. `copy-form` on a variation makes a plain form from it (e.g. to keep the winner).

Quirks worth knowing before you edit a real form:
- Every save must send the live version back exactly as returned, and Klaviyo often rejects it anyway: editor-built forms carry things its own validator refuses (empty layout rows, URL rules with no URLs, two teasers on one device type), and publishing from the editor does not clean them up. When that happens: `unpublish-form`, save the draft (now the only version), `publish-form`. Same form ID; the form is off the site for a few seconds; if the save fails, republish the untouched draft to restore it.
- `unpublish-form` deletes the live version rather than pausing it. To pause a form, toggle it off in the Klaviyo UI.
- Every publish creates a new live version ID (the editor does the same).
- Drafts are cleaned automatically before saving (empty rows, empty URL rules); `copy-form` also drops a second same-device teaser and says so.
- Survey/checkbox options have a `label` and a `value`; the value is what lands on the profile, so change both.
- Delete a form with `klaviyo delete form <id>`.

| Command | Description |
|---|---|
| `form` | Show a form's versions, triggers, and every block's text step by step; `--out` saves the full definition JSON for editing |
| `form-replace` | Find/replace any text (copy, labels, URLs) in the draft version(s); `--dry-run` shows each change; verifies by reading back. Never touches the live version |
| `update-form` | Save an edited `form --out` file back as the definition; live versions are restored from the server and version IDs must match |
| `copy-form` | Create a new draft form from one version of an existing form (`--version`, default the live one; an A/B variation copies out as a plain form) |
| `ab-test-form` | Create a new draft A/B test form, one variation per `--variant` (default A and B), each copied from one version; start it in the form editor |
| `publish-form` | Publish the draft over the live version; requires the form ID typed back or `--confirm <id>` |
| `unpublish-form` | Take a form off the site; same confirmation |

### SMS
| Command | Description |
|---|---|
| `upload-sms` | Create an SMS campaign draft in Klaviyo |

### Images
| Command | Description |
|---|---|
| `upload-image` | Upload a local image to the image library and print its hosted URL (multipart; needs a direct API key) |

### Delete
| Command | Description |
|---|---|
| `delete` | Permanently delete a segment, list, campaign, flow, or form: `klaviyo delete <type> <id>`. Shows what uses it first, then requires the ID typed back or `--confirm <id>` |

### Raw API
| Command | Description |
|---|---|
| `api` | Raw API pass-through: `klaviyo api <METHOD> <path>` |

That's 50 commands total.

## Multi-account profiles

For managing more than one Klaviyo account, put credentials in `~/.config/klaviyo-cli/config.toml`:

```toml
default_profile = "acme"

[profiles.acme]
api_key = "pk_acme_..."

[profiles.other-brand]
api_key = "pk_other_..."
```

Select a profile with `--profile` (or `-p`), or set `KLAVIYO_PROFILE`:

```bash
klaviyo --profile other-brand account-health
KLAVIYO_PROFILE=other-brand klaviyo account-health
```

Precedence: an explicit `--profile` (or `KLAVIYO_PROFILE`) wins first. Otherwise `KLAVIYO_API_KEY` is used if set. Otherwise the CLI falls back to `default_profile` in the config file.

Agencies running the CLI across many clients can go further and embed it: re-expose every command under a host CLI with per-client auth resolution, so `yourcli campaign-performance acme --days 30` resolves credentials for `acme` before the call. See the `wrap_with_account` and `build_host_group` docstrings in `src/klaviyo_cli/embed.py`.

## License

MIT. See [LICENSE](LICENSE).

Built and maintained by [BS&Co](https://bsandco.us), a retention marketing agency for eCommerce brands.

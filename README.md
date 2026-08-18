# klaviyo-cli

A command-line interface for Klaviyo: campaigns, segments, flows, metrics, and scheduling. Built for humans and AI agents.

```bash
uvx --from klaviyo-cli klaviyo --help
```

No install needed. Or install it: `pipx install klaviyo-cli` or `uv tool install klaviyo-cli`. Requires Python 3.11+.

Built and maintained by [BS&Co](https://bsandco.us), a retention marketing agency for eCommerce brands. Read [why we built it and how we made it safe to hand to AI agents](https://bsandco.us/blog-post/klaviyo-cli).

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

Destructive Klaviyo operations are blocked at the CLI level. Klaviyo's DELETE endpoints for profiles, lists, segments, campaigns, and flows are not reachable through this tool, even via the raw `api` passthrough. Only template deletion is allowlisted (`transport.py`, `DELETE_ALLOWED_PATHS`). This is why it's safe to hand `klaviyo-cli` to an agent with a live API key: the agent can read anything and change campaign content, timing, and audiences, but it cannot delete subscriber data, segments, or send history. Two commands can stop mail going to real people, and both require an explicit `--yes` flag or an interactive confirmation before they run: `suppress` (reversible — `unsuppress` undoes it) and `unsubscribe` (it revokes consent, which `unsuppress` cannot undo; reversing it takes a deliberate call to Klaviyo's bulk subscribe endpoint asserting fresh consent, which this CLI intentionally does not wrap).

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
| Command | Description |
|---|---|
| `list-campaigns` | List/filter campaigns by status, channel, and date (beyond name search) |
| `search-campaigns` | Search campaigns by name |
| `get-campaign` | Show details for a specific campaign |
| `get-creative` | Dump a campaign's creative (subject + text/HTML) via its template |
| `list-drafts` | List draft email campaigns for a client |
| `patch-campaign` | Update campaign send time and/or audiences |
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
| `events` | List recent events for a metric ID; `--since/--until` windows, `--properties` payloads |
| `export-events` | Bulk-export ALL events for a metric in a window as NDJSON (exhaustive pagination; `--out` file, `--fields`, `--max-pages` guard) |

### Metrics
| Command | Description |
|---|---|
| `account-health` | Show profiles count, lists, and metrics for a client |
| `list-metrics` | List event metrics with their IDs and integration (ID<->name catalog) |
| `metric-aggregate` | Bucketed counts for one metric over time (hour/day/week/month, optional group-by) |
| `form-performance` | Show pop-up/form views, submits, and submit rates |

### SMS
| Command | Description |
|---|---|
| `upload-sms` | Create an SMS campaign draft in Klaviyo |

### Raw API
| Command | Description |
|---|---|
| `api` | Raw API pass-through: `klaviyo api <METHOD> <path>` |

That's 32 commands total.

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

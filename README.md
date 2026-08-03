# klaviyo-cli

A command-line interface for Klaviyo: campaigns, segments, flows, metrics, and scheduling. Built for humans and AI agents.

```bash
uvx klaviyo-cli --help
```

No install needed. Or install it: `pipx install klaviyo-cli` or `uv tool install klaviyo-cli`. Requires Python 3.11+.

Built and maintained by [BS&Co](https://bsandco.us), a retention marketing agency for eCommerce brands.

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

Every command supports `--json` for machine-readable output:

```bash
klaviyo list-campaigns --days 30 --json
```

Destructive Klaviyo operations are blocked at the CLI level. Klaviyo's DELETE endpoints for profiles, lists, segments, campaigns, and flows are not reachable through this tool, even via the raw `api` passthrough. Only template deletion is allowlisted (`transport.py`, `DELETE_ALLOWED_PATHS`). This is why it's safe to hand `klaviyo-cli` to an agent with a live API key: the agent can read anything and change campaign content, timing, and audiences, but it cannot delete subscriber data, segments, or send history.

Drop this in your repo's `CLAUDE.md` so an agent knows the tool exists:

```markdown
## Klaviyo
Use the `klaviyo` CLI for Klaviyo data and actions (campaigns, segments,
flows, metrics). Auth via KLAVIYO_API_KEY env var. Every command supports
--json. Run `klaviyo --help` for the full command list.
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
| `flows` | List all flows for a client |
| `flow-detail` | Show full flow structure: trigger, filters, emails with subjects, delays, splits |
| `flow-performance` | Show flow revenue and engagement metrics |

### Metrics
| Command | Description |
|---|---|
| `account-health` | Show profiles count, lists, and metrics for a client |
| `list-metrics` | List event metrics with their IDs and integration (ID<->name catalog) |
| `form-performance` | Show pop-up/form views, submits, and submit rates |

### SMS
| Command | Description |
|---|---|
| `upload-sms` | Create an SMS campaign draft in Klaviyo |

### Raw API
| Command | Description |
|---|---|
| `api` | Raw API pass-through: `klaviyo api <METHOD> <path>` |

That's 23 commands total.

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

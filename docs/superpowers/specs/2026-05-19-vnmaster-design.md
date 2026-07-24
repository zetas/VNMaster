# VNMaster — Design Spec

**Date:** 2026-05-19
**Status:** Approved for implementation planning
**Target platform:** macOS (Apple Silicon and Intel)

## 1. Problem statement

Once a week the user manually scrolls F95Zone's Latest Updates feed with tag filters applied, trying to remember:

- "Did I play this game?"
- "When did I last play it?"
- "How much has been added since then — is it worth re-downloading?"

This is error-prone and exhausting. F95Checker (an existing open-source tool) covers some of this, but two gaps remain critical for this user:

1. The user has played dozens of Ren'Py games over time but does not keep them all installed. F95Checker does not auto-detect what the user has played — it requires manual per-game registration.
2. F95Checker displays raw changelog text only. It does not quantify the magnitude of changes (renders, animations, words, scenes added), which is the user's actual decision input for "worth re-downloading."

## 2. Goals and non-goals

### Goals (v1.0)

- Build a companion tool that reads from F95Checker and the user's local filesystem, and produces a weekly Discord digest answering both questions above.
- Detect every Ren'Py game the user has ever played (via `~/Library/RenPy/` save-data inspection), regardless of whether the game is currently installed.
- Parse F95 changelogs into structured magnitude data via Anthropic Claude Haiku.
- Provide a Discord reaction loop for hiding, marking-interested, or skipping suggestions.
- Surface new F95 games matching the user's tag preferences (discovery feed).
- Run automatically on a weekly schedule via `launchd`.

### Non-goals (v1.0)

- Replacing F95Checker. VNMaster relies on F95Checker for: scraping, version tracking, game launching, browser-extension capture, and the live GUI.
- Auto-downloading games. The digest links out; the user downloads manually.
- Cross-platform support. macOS only.
- Mid-week instant alerts (deferred to v1.1).
- A VNMaster GUI of its own. Discord is the only user interface.
- Supporting non-Ren'Py engines.

## 3. Architecture overview

VNMaster is a Python service composed of two long-running pieces and one batch pipeline.

```
                       ┌────────────────────────┐
                       │ launchd timer          │
                       │ (weekly, Sat 9 AM)     │
                       └────────────┬───────────┘
                                    │ fires
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ vnmaster digest  (batch pipeline, one-shot per run)                │
│                                                                    │
│   1. Play-history scanner       — reads ~/Library/RenPy/           │
│   2. Disk scanner               — reads ~/Games/                   │
│   3. F95Checker DB reader       — reads F95Checker SQLite (RO)     │
│   4. Library matcher            — joins all three, resolves        │
│                                   thread IDs via fuzzy match       │
│   5. Discovery fetcher          — hits api.f95checker.dev/latest   │
│   6. Changelog magnitude extractor                                 │
│                                   — sends raw changelogs to        │
│                                     Anthropic Haiku, gets          │
│                                     structured deltas              │
│   7. Digest builder             — assembles two-section Discord    │
│                                   embed bundle                     │
│   8. Discord poster             — webhook + bot client posts       │
│                                                                    │
│   Writes to: ~/Library/Application Support/VNMaster/vnmaster.db    │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│ vnmaster bot     (launchd-managed daemon, runs at login)           │
│                                                                    │
│   - Connects to Discord gateway (discord.py)                       │
│   - Listens for reactions in the user's configured channel only    │
│   - Maps reactions → state changes in vnmaster.db                  │
│   - Responds to slash commands: /vnm tags, /vnm pair, /vnm status  │
│   - Sends DMs on ♻️ (re-download interest) and ✅ (add-to-tracking) │
└────────────────────────────────────────────────────────────────────┘
```

### Data sources (all read-only from VNMaster's perspective)

| Source | Path / URL | Purpose |
|--------|------------|---------|
| Filesystem (games) | `~/Games/` | Currently-installed games + on-disk versions |
| Filesystem (saves) | `~/Library/RenPy/` | Historical play data (last-played, save counts) |
| F95Checker DB | `~/Library/Application Support/F95Checker/db.sqlite3` | Thread IDs, upstream versions, raw changelogs, user metadata |
| F95Indexer API | `https://api.f95checker.dev/latest` | Discovery feed |
| Anthropic API | `https://api.anthropic.com` | Changelog magnitude extraction |
| Discord Gateway + Webhook API | `discord.com` | Digest delivery + reaction loop |

VNMaster never writes to F95Checker's DB. It owns exactly one writable store: `vnmaster.db`.

## 4. Component specifications

### 4.1 Play-history scanner

**Input:** Path to `~/Library/RenPy/` (configurable).

**Behavior:** For each subdirectory found:

- Compute `save_dir_name` = directory basename.
- Compute `last_played_at` = max mtime of `*.save` files. If none exist, set to null.
- Compute `first_played_at` = min mtime of `*.save` files.
- Compute `save_count` = count of `*.save` files.
- Compute `total_save_size_bytes` = sum of file sizes in the dir.
- Detect `persistent_data_present` = boolean (file named `persistent` exists).

**Output:** List of `PlayHistoryEntry` records.

**Edge cases:**
- Empty save dir (no `*.save` files) → still recorded; flagged as "registered but never saved."
- Symlinks → followed; warning logged if cycle detected.
- Permission denied → skipped with warning to log; never crashes the scan.

### 4.2 Disk scanner

**Input:** Path to `~/Games/` (configurable).

**Behavior:** For each subdirectory one level deep:

- Identify Ren'Py installs by presence of all three: `renpy/` subdir, `game/` subdir, at least one launcher file (`*.app`, `*.sh`, or `*.exe`).
- Skip dirs that don't match.
- Extract `installed_version` using a three-tier strategy, taking the first that succeeds:
  1. Regex on folder name: `(\d+(?:\.\d+)+[a-z]?)` after a separator (`-`, `_`, ` `).
  2. Parse `game/options.rpy`, find line matching `config.version = "..."`.
  3. Return `"unknown"`.
- Extract `save_dir_hint` by parsing `config.save_directory` from `game/options.rpy`. Used to bind disk-scanner output to play-history-scanner output at the matching stage.
- Compute `disk_size_bytes` via recursive `os.walk`.

**Output:** List of `InstalledGame` records.

**Edge cases:**
- Symlinks to game files outside `~/Games/` → followed.
- `game/options.rpy` that contains the version in a dynamically computed string → falls back to tier 3.
- Game folders with mods/extra files alongside the `.app` → still detected (we look for the Ren'Py structure, not exclusivity).

### 4.3 F95Checker DB reader

**Connection:** SQLAlchemy with `sqlite:///<path>?mode=ro&uri=true`.

**Schema check:** On startup, verify the following columns exist on the games table: `id`, `name`, `version`, `developer`, `engine`, `status`, `last_updated`, `changelog`, `description`, `image_url`, `tags`, `executable`, `archived`, `rating`. Compare against known F95Checker 11.x schema. On mismatch, emit a digest-channel message: "F95Checker schema changed since VNMaster <version>. Check for VNMaster update at <repo URL>." and abort the run.

**Per-game query:** Returns all the columns above for use in matching and changelog extraction.

**Delta query:** `WHERE last_updated > :last_digest_run_at` for the weekly diff. Used to identify which games need fresh magnitude extraction.

### 4.4 Library matcher

**Input:** Output of all three scanners + F95Checker DB rows.

**Behavior:** Produces one merged `LibraryGame` record per F95 thread the user has any relationship with. The match key is `f95_thread_id`.

**Matching strategy:**

1. **Disk → F95Checker**: prefer F95Checker's stored `executable` path (the user can set this in F95Checker per game). Fall back to fuzzy title match between `InstalledGame.folder_name` and `F95CheckerGame.name` using rapidfuzz `token_set_ratio`. Threshold for auto-match: ≥ 90. Below threshold, the disk entry is logged as "unmatched_installed" and surfaced in the digest.

2. **Save dir → F95Checker**: same fuzzy match between `PlayHistoryEntry.save_dir_name` and `F95CheckerGame.name`. Same 90 threshold.

3. **Cross-binding disk and save dir**: when a disk entry has `save_dir_hint`, use it to look up the matching `PlayHistoryEntry` and merge the two into a single library row.

4. **Cached pairings**: any pairing the user has manually confirmed (via `/vnm pair` slash command or auto-match above threshold) is cached in `vnmaster.db.pairings`. Cached pairings always win over fresh fuzzy matches in subsequent runs.

**Output:** Writes/upserts to `library_games` table. Schema:

```sql
CREATE TABLE library_games (
  f95_thread_id INTEGER PRIMARY KEY,
  game_title TEXT NOT NULL,
  -- Play history (null if user has never played)
  save_dir_name TEXT,
  last_played_at INTEGER,
  first_played_at INTEGER,
  save_count INTEGER,
  total_save_size_bytes INTEGER,
  -- Currently installed (null if not on disk now)
  install_path TEXT,
  installed_version TEXT,
  disk_size_bytes INTEGER,
  -- F95Checker mirror (refreshed every run)
  latest_upstream_version TEXT,
  upstream_last_updated_at INTEGER,
  upstream_thread_url TEXT,
  raw_changelog TEXT,
  tags_json TEXT,
  status TEXT,
  developer TEXT,
  image_url TEXT,
  -- User reaction state
  hidden INTEGER DEFAULT 0,
  interested INTEGER DEFAULT 0,
  acknowledged_version TEXT,  -- set via 📥; if latest_upstream_version equals this, suppress in digest
  last_seen_in_digest_at INTEGER,
  -- Audit
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE pairings (
  f95_thread_id INTEGER PRIMARY KEY,
  save_dir_name TEXT,
  folder_name TEXT,
  confidence REAL NOT NULL,  -- 1.0 = manual confirmation
  paired_at INTEGER NOT NULL
);
```

### 4.5 Discovery fetcher

**Input:** User's tag filters from config, list of `f95_thread_id`s already in `library_games`, set of hidden/skip-until threads.

**Behavior:**

1. GET `https://api.f95checker.dev/latest?days=7`.
2. For each candidate thread, apply filter pipeline (in order):
   - Drop if a `library_games` row exists with this `f95_thread_id` (user already tracks it).
   - Drop if `discovery_state.hidden = 1` for this `f95_thread_id`.
   - Drop if `discovery_state.skip_until > now()` for this `f95_thread_id`.
   - Drop if no tag intersects with `discovery.include_tags` (ANY semantic).
   - Drop if any tag intersects with `discovery.exclude_tags` (ALL semantic — any single match excludes).
3. Rank by: `recency_score * 0.4 + likes_score * 0.4 + tag_match_count * 0.2`, where:
   - `recency_score = exp(-age_days / 7)` (1.0 if posted today, ~0.37 if a week old, ~0.14 if two weeks old)
   - `likes_score = min(1.0, log10(max(likes, 1)) / 3)` (saturates at 1000 likes)
   - `tag_match_count` normalized to 1.0 (count of intersecting include-tags divided by the user's total include-tag count)
   Take top N (default 10).
4. Upsert into `discovery_suggestions` table.

**Schema:**

```sql
CREATE TABLE discovery_suggestions (
  f95_thread_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  developer TEXT,
  posted_at INTEGER NOT NULL,
  likes INTEGER,
  tags_json TEXT,
  short_description TEXT,
  image_url TEXT,
  first_seen_at INTEGER NOT NULL,
  last_seen_in_digest_at INTEGER
);

CREATE TABLE discovery_state (
  f95_thread_id INTEGER PRIMARY KEY,
  hidden INTEGER DEFAULT 0,
  skip_until INTEGER,  -- epoch seconds; NULL = no skip
  interested INTEGER DEFAULT 0,
  updated_at INTEGER NOT NULL
);
```

### 4.6 Changelog magnitude extractor

**Input:** Raw changelog text + game's title for context.

**Behavior:**

1. Compute `sha256(raw_changelog)`. If a matching hash is already in `changelog_extractions`, reuse the cached structured result. No API call.
2. Otherwise, call Anthropic Claude Haiku via the SDK with prompt caching enabled on the system prompt. The system prompt is a fixed instruction + JSON schema document. The user prompt is just the changelog text + title.
3. Use Anthropic's tool-use feature to enforce structured output. Tool name: `record_changelog`.
4. Schema:

```json
{
  "versions": [
    {
      "version": "string (the version label, e.g., '0.7.0')",
      "released_at": "string|null (ISO date if mentioned)",
      "renders": "integer|null",
      "animations": "integer|null",
      "words": "integer|null",
      "scenes": "integer|null",
      "new_locations": "integer|null",
      "new_characters": "integer|null",
      "bugfix_only": "boolean",
      "summary_one_line": "string (≤80 chars, dev's perspective)"
    }
  ]
}
```

5. Persist to `changelog_extractions` keyed by `(f95_thread_id, content_hash)`.

**Fallback:** If the Anthropic API errors or the monthly budget cap is hit, run a regex-based extractor against common patterns: `(\d+)\s+(new\s+)?renders?`, `(\d+)\s+animations?`, `(\d+(?:,\d+)?)\s+(?:lines?|words?)\s+of\s+dialogue`, etc. Records the result with `extraction_method = 'regex_fallback'`. Lower fidelity, no `summary_one_line`.

**Schema:**

```sql
CREATE TABLE changelog_extractions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  f95_thread_id INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  extraction_method TEXT NOT NULL,  -- 'llm' | 'regex_fallback'
  versions_json TEXT NOT NULL,
  extracted_at INTEGER NOT NULL,
  UNIQUE (f95_thread_id, content_hash)
);
```

**Magnitude score** (used only for ranking digest entries):

```
score(v) = renders * w_renders
        + animations * w_animations
        + (words / 1000) * w_words_per_1k
        + scenes * w_scenes
        + new_locations * w_new_locations
        + new_characters * w_new_characters
        + (w_bugfix_only_penalty if bugfix_only else 0)
```

Weights are loaded from `config.magnitude_score`. Defaults:

```
renders = 1.0
animations = 5.0
words_per_1k = 1.0
scenes = 50.0
new_locations = 20.0
new_characters = 10.0
bugfix_only_penalty = -100.0
```

Score per *game* in the digest is the sum of `score(v)` for all versions strictly newer than the user's `installed_version` (or all versions if the user has never played).

Stars in the digest are a coarse mapping: `<10 = ★`, `<50 = ★★`, `<200 = ★★★`, `<800 = ★★★★`, `≥800 = ★★★★★`. These bands are not configurable in v1.0; only the underlying weights are.

### 4.7 Digest builder

**Input:** Snapshot of `library_games`, `discovery_suggestions`, `changelog_extractions`, configuration.

**Behavior:**

1. Select library games where `hidden = 0` AND `(acknowledged_version IS NULL OR acknowledged_version != latest_upstream_version)` AND any of:
   - `upstream_last_updated_at > previous_digest_run_at` (new update since last digest), OR
   - `installed_version` is set AND `installed_version != latest_upstream_version` AND this game has not appeared in any digest in the past `max_repeat_weeks` weeks (default 4) — catches games the user is still behind on even if no fresh release happened this week.
2. For each, compute `score_since_user_version` (per §4.6).
3. Sort updates descending by magnitude score.
4. Select discovery suggestions per §4.5.
5. Render two `EmbedBundle` objects: `UpdatesEmbedBundle` (one embed per game, capped at 25 — Discord per-message limit) and `DiscoveryEmbedBundle` (similar).
6. If updates list exceeds 25, split across multiple messages with continuation headers.

**Embed shape** for an updated game (matches the approved mockup):

- Color: amber (`#f0b232`)
- Title: `{game_title} — v{latest} (yours: v{installed_version or 'never played'})`
- Description: `{developer} · Ren'Py · {status} · last played {date} · {installed_status}`
- Field "Since you last played": multiline with renders/animations/words/scenes (omits null fields)
- Field "Magnitude score": `★`-rendered stars + dev `summary_one_line`
- Footer: links to F95 thread and direct download (first MEGA/MIXDROP link from F95Checker's `download_links_json`)
- Thumbnail: `image_url` from F95Checker
- Bot adds reactions ♻️ ❌ 📥 to the embed message

**Embed shape** for a discovery suggestion:

- Color: green (`#23a55a`)
- Title: `{title} — v{version} [NEW]`
- Description: `{developer} · Ren'Py · {status} · posted {date} · {likes} likes`
- Field "Tags matching your filters": intersect of game tags with `discovery.include_tags`
- Field "Overview": first 200 chars of `short_description`
- Footer: F95 thread link
- Bot adds reactions ✅ ❌ ⏭️

### 4.8 Discord poster

**Behavior:**

1. POST kickoff message via webhook (simple text).
2. For each embed bundle, POST via webhook with up to 10 embeds per message.
3. Capture each posted message's ID.
4. For each embed in each message, add appropriate reactions via the bot's authenticated client (webhooks can't add reactions).
5. Insert `digest_runs` and `digest_entries` records associating message IDs with `f95_thread_id`s, so the bot can resolve reactions back to games.

**Schema:**

```sql
CREATE TABLE digest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_at INTEGER NOT NULL,
  updates_count INTEGER NOT NULL,
  discovery_count INTEGER NOT NULL,
  llm_calls INTEGER NOT NULL,
  llm_cost_usd REAL NOT NULL
);

CREATE TABLE digest_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES digest_runs(id),
  discord_message_id TEXT NOT NULL,
  embed_index INTEGER NOT NULL,
  kind TEXT NOT NULL,  -- 'update' | 'discovery'
  f95_thread_id INTEGER NOT NULL,
  UNIQUE (discord_message_id, embed_index)
);
```

### 4.9 Discord reaction bot (daemon)

**Lifecycle:** launchd-managed agent at `~/Library/LaunchAgents/dev.vnmaster.bot.plist`. Starts at login. Restarts on crash with exponential backoff capped at 5 minutes.

**Connection:** discord.py 2.x with a bot token. Intents: `messages`, `reactions`, `dm_messages`. Watches only the configured `guild_id` + `channel_id` — ignores everything else.

**Reaction event handling:** On `on_raw_reaction_add`:

1. Lookup `digest_entries` by `(message_id, embed_index)`. (Discord doesn't give us per-embed reactions, so we treat the entire message's reactions as applying to embed 0 only if multiple embeds. Implementation note: post one embed per message to avoid ambiguity. Tradeoff: more messages but unambiguous reactions.)
2. Resolve emoji to action:
   - **Update embeds**: ♻️ → `library_games.interested = 1` + DM user with download links · ❌ → `library_games.hidden = 1` (permanent; never appears in any future digest) · 📥 → record current `latest_upstream_version` as `acknowledged_version`; this version stops appearing in updates digests, but if a newer version drops later, it shows up again

   - **Discovery embeds**: ✅ → `discovery_state.interested = 1` + DM user with F95 thread URL and "drag this into F95Checker" instructions · ❌ → `discovery_state.hidden = 1` · ⏭️ → `discovery_state.skip_until = now + 4 weeks`
3. All writes go through a single `apply_reaction(thread_id, kind, action)` function for testability.

**Slash commands:**

- `/vnm tags add <tag>` and `/vnm tags remove <tag>` and `/vnm tags list`
- `/vnm pair <save_dir_or_folder_name> <f95_url>` — manually pair an unmatched local entity to an F95 thread
- `/vnm status` — show last digest run, llm cost MTD, unmatched count, db size
- `/vnm digest now` — trigger an out-of-schedule digest run (calls the same pipeline as launchd)

Implementation note: the architectural separation of "single embed per message" vs "multiple embeds per message" was decided in favor of single-per-message because it makes reaction routing unambiguous. The user trades visual density for correctness. This is acceptable for the use case (low-frequency, async consumption).

## 5. Configuration

`~/.config/vnmaster/config.toml`:

```toml
[paths]
games_root = "~/Games"
renpy_saves_root = "~/Library/RenPy"
f95checker_db = "~/Library/Application Support/F95Checker/db.sqlite3"
vnmaster_db = "~/Library/Application Support/VNMaster/vnmaster.db"

[discord]
guild_id = ""
channel_id = ""
webhook_url = ""
# bot_token in secrets.toml

[anthropic]
model = "claude-haiku-4-5"
monthly_budget_usd = 5.0
# api_key in secrets.toml

[schedule]
cron = "0 9 * * SAT"

[discovery]
include_tags = []
exclude_tags = []
max_suggestions_per_digest = 10
skip_window_weeks = 4
discovery_lookback_days = 7

[matching]
fuzzy_threshold = 90

[magnitude_score]
renders = 1.0
animations = 5.0
words_per_1k = 1.0
scenes = 50.0
new_locations = 20.0
new_characters = 10.0
bugfix_only_penalty = -100.0
```

`~/.config/vnmaster/secrets.toml` (chmod 600):

```toml
discord_bot_token = ""
anthropic_api_key = ""
```

## 6. First-run wizard

`vnmaster init` interactive flow:

1. Confirm path defaults (offer to override each).
2. Verify F95Checker is installed (db file exists at the configured path). If not, instruct the user to install it from <https://github.com/WillyJL/F95Checker> and exit with a "re-run after F95Checker is set up" message.
3. Prompt for Anthropic API key. Test it with a one-token request before continuing.
4. Prompt for Discord bot token, guild ID, channel ID, and webhook URL. Test the webhook with a "VNMaster init starting" message; verify the bot can connect to the gateway and read messages in that channel.
5. Run play-history scanner. Report: "Found 87 played games."
6. Run disk scanner. Report: "Found 14 installed games."
7. Cross-bind disk hints to save dirs via `config.save_directory` parsing.
8. Generate `~/.config/vnmaster/import-candidates.txt`: one F95 thread URL per detected game, with a confidence flag and short comment. Each line of the form `# <confidence> | <game_title> | <hint_from>` followed by the URL on the next line. The user reviews, deletes any wrong matches, and pastes the remaining URLs into F95Checker's bulk-import dialog. (Step blocks until the user confirms "done.")
9. Re-query F95Checker DB. Run the library matcher. Report match rates ("matched 78 of 87 played games to F95 threads; 9 unmatched — pair with `/vnm pair` after setup").
10. Collect tag preferences: bot DMs the user a paginated list of F95 tags currently in use (pulled from `api.f95checker.dev`) with Discord component buttons. User ticks include-tags and exclude-tags; saved to `config.toml`.
11. Send a synthetic "init complete" test digest containing one fake update entry + one fake discovery entry to verify Discord wiring end-to-end (including reactions and slash commands).
12. Install launchd plists: `dev.vnmaster.weekly.plist` (the cron job) and `dev.vnmaster.bot.plist` (the daemon). Start both.

## 7. Testing strategy

TDD, pytest. Test pyramid: many unit tests, some integration, two end-to-end.

**Unit tests:**
- Play-history scanner: fixture dirs (empty, well-formed, malformed, symlinked, permission-denied).
- Disk scanner: fixture dirs with valid Ren'Py installs, non-Ren'Py folders, dirs with `options.rpy` variations.
- F95Checker DB reader: fixture SQLite file with known rows; schema-drift fixtures.
- Library matcher: synthetic combinations of scanner outputs + DB rows; verify cached-pairing precedence.
- Discovery filter pipeline: pure function, exhaustive parametrized tests covering each filter step.
- Magnitude score: tabletop tests with hand-computed expected scores.
- Digest builder: snapshot tests on generated embed JSON.
- Reaction handler: parametrized tests on `apply_reaction()` covering all 6 reaction types × valid/invalid state.

**Integration tests:**
- Changelog extractor against a golden corpus of 20 real F95 changelog excerpts (anonymized titles). Pinned LLM responses cached in repo for CI; real-API run guarded by env flag (`VNMASTER_LIVE_LLM=1`).
- Full pipeline run against a synthetic temp filesystem + fixture F95Checker DB + mocked Discord client.
- launchd plist installation/removal.

**End-to-end (manual + scripted):**
- A `vnmaster e2e` command that runs init wizard against an isolated test config dir, posts a digest to a configurable test Discord channel, and exits.

**Coverage targets:** ≥ 90% on the matcher, scanner, filter, and magnitude score modules. ≥ 70% on the rest.

## 8. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| F95Checker schema drift breaks DB reader | Medium | High | Pin schema version, check on every run, surface clear error in Discord. |
| F95 changelog format too variable for LLM | Low | Medium | LLM proved robust to format variance; regex fallback for edge cases. |
| `api.f95checker.dev` discontinued | Low | Medium | Fall back to direct F95Zone scraping; library tracking continues unaffected (it uses F95Checker's local DB). |
| Save dir names don't match game titles | Medium | Low | Surface unmatched in digest; `/vnm pair` slash command for manual binding. |
| Anthropic API outage or cost spike | Low | Medium | Monthly USD budget cap with regex fallback when exceeded. |
| Discord bot rate limits | Low | Low | discord.py handles automatically; digest spans seconds, not minutes. |
| `~/Library/RenPy/` save dir naming changes across Ren'Py versions | Low | Low | Surface unmatched dirs each run; pair manually. Re-runs are idempotent. |

## 9. Implementation language and dependencies

- **Language:** Python 3.12+.
- **Key dependencies:**
  - `anthropic` (LLM extraction)
  - `discord.py` 2.x (bot + slash commands)
  - `httpx` (api.f95checker.dev calls)
  - `rapidfuzz` (fuzzy match)
  - `sqlalchemy` (DB access)
  - `pydantic` v2 (config + structured types)
  - `tomli` / `tomli_w` (config files)
  - `pytest`, `pytest-asyncio`, `pytest-mock`, `hypothesis` (testing)
- **Packaging:** `pyproject.toml` + `uv` for dependency management. Single CLI entrypoint via `vnmaster` console script.
- **Distribution:** Local install via `uv tool install .` from a checked-out repo. Not published to PyPI for v1.0.

## 10. User-provided prerequisites

To run VNMaster, the user must provide once during `init`:

1. **Anthropic API key** — from console.anthropic.com.
2. **Discord bot** — registered application + bot user + token + invited to user's server (scopes: `bot`, `applications.commands`; permissions: Read Messages, Send Messages, Add Reactions, Embed Links, Use External Emoji, Read Message History).
3. **Private Discord channel** ID + a webhook URL for that channel.
4. **F95Checker** installed, with at least one game added so we can verify DB reads.
5. **Tag filter preferences** (include + exclude lists) — collected interactively during init.

## 11. Open items deferred to implementation

- Exact `import-candidates.txt` format (TSV vs YAML vs human-readable) — implementer's choice during planning.
- Whether `/vnm digest now` posts to the same channel as the scheduled digest or to a configurable "preview" channel.
- Whether to keep last N digests' embeds editable (e.g., update magnitude bars retroactively if changelogs are revised) — likely no for v1.0.
- Exact thresholds for star ratings (currently hardcoded in §4.6) — may be promoted to config if the defaults feel wrong in practice.

## 12. Versioning and rollout

- **v1.0** — everything in §4.
- **v1.1 candidates**: mid-week instant alerts for high-magnitude updates; auto-add-to-F95Checker via the browser extension's local endpoint if reverse-engineerable; per-game scoring tuning via slash command.
- **Backward compatibility:** `vnmaster.db` migrations via Alembic. Each release bumps a schema version. Downgrade not supported.

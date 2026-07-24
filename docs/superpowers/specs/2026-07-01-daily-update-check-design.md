# Daily update check — design

## Goal

Add a nightly run (default 1am, daily) that detects newly-available upstream
updates for tracked games and alerts Discord with an `@everyone` ping plus the
same embeds the weekly digest produces. When there is nothing new, the daily
run posts nothing at all. The Saturday weekly digest keeps its current
behavior unchanged.

## Behavior summary

- `vnmaster digest --daily` runs the same pipeline as the weekly digest, in
  `mode="daily"`. Scan, match, upsert, select, LLM extract, post, and record
  all reuse the existing code; the mode only gates the differences below.
- Daily alerts fire for two kinds of change, each deduped independently so the
  same change never re-alerts on later nights:
  - a **version bump** the user is behind on, and
  - a **notable status change** (Completed / On Hold / Abandoned / resumed),
    using the existing `status.status_changed()` notability rules.
- Empty night = zero LLM calls and no Discord post.
- Non-empty night = one webhook post prefixed with `@everyone` (allowed
  mentions set so the ping actually delivers), followed by the same update
  embeds and `⬇️`/`📦` reactions as the weekly digest.

## Keeping the weekly report unchanged

The weekly selection depends on state a daily run must not disturb:

1. **"Since last digest" pointer.** `_previous_run_at()` returns the max
   `digest_runs.run_at`. Add a `kind` column to `digest_runs`
   (`'weekly'` / `'daily'`, default `'weekly'`) and make `_previous_run_at()`
   filter to `kind='weekly'`. Daily runs are recorded but never move the
   weekly pointer.
2. **`last_seen_in_digest_at`** (weekly repeat throttle). Daily never stamps it.
3. **`status` / `status_changed`.** The weekly banner is computed at upsert by
   diffing the stored status against the live F95 status. If a daily upsert
   overwrote the stored status it would consume that transition and the weekly
   would miss it. So the daily upsert runs with `write_status=False`: it never
   writes `status` or `status_changed`. Daily reads the live status straight
   from the F95Checker rows it already has in hand (see selection below), so it
   still detects status changes without touching weekly state.

Everything else the upsert refreshes (versions, changelog, timestamps, tags,
developer, image, URL) is an idempotent copy from the same F95Checker/disk
sources the weekly run reads anyway, so an earlier daily refresh is invisible
to the weekly run.

Net effect: a game alerted at 1am still appears in Saturday's digest exactly as
it would have.

## Daily dedup state

Two new columns on `library_games`:

- `last_daily_notified_version` — last upstream version alerted via a daily run.
- `last_daily_notified_status` — last F95 status alerted via a daily run.

Both are baselined on insert (to the current `latest_upstream_version` /
`status`) by `_upsert_library`, and backfilled for existing rows in the
migration. This makes the first night after deploy intentionally quiet: daily
only fires on changes that happen *after* the baseline, so the existing backlog
does not produce an `@everyone` storm. The weekly digest continues to carry the
backlog.

## Daily selection

New function `select_daily_candidates(engine, f95_rows, now_epoch)` in
`digest/select.py`. It loads `library_games` and, for each tracked row, reads
the live upstream version and status from the matching F95Checker row. A game
is a daily candidate when:

- not `hidden`, and
- `acknowledged_version != latest_upstream_version`, and
- **version signal:** user is behind (same `is_user_behind` predicate the
  weekly uses) AND `latest_upstream_version != last_daily_notified_version`,
  OR
- **status signal:** `status_changed(last_daily_notified_status, live_status)`
  is true.

It returns the same `SelectedUpdate` shape the weekly path uses, so embed
building and posting are shared. Selection runs before any LLM call, so a quiet
night is cheap.

Games with no `installed_version` (played but not on disk) are covered by the
weekly digest's date-based fallback, not by the daily version signal — daily's
version dedup needs a concrete version to compare. Such a game can still fire a
daily alert through the status signal.

## Recording a daily run

After posting, in `mode="daily"`:

- insert `DigestRun(kind='daily', ...)` and the `DigestEntry` rows (needed so
  reactions on the alert resolve to a game via `discord_message_id`),
- set `last_daily_notified_version = latest_upstream_version` and
  `last_daily_notified_status = status` for each alerted game,
- do **not** stamp `last_seen_in_digest_at`.

The weekly `mode="weekly"` record path is unchanged except for tagging its
`DigestRun` with `kind='weekly'`.

## Posting

`DiscordPoster` gains an optional `mention_everyone: bool`. When set (daily),
it prepends `@everyone` to the kickoff content and passes an allowed-mentions
value so the webhook delivers the ping. Weekly posting is unchanged. When there
are no candidates in daily mode, the pipeline returns before posting anything —
no kickoff line.

## Scheduling

- Config: add `schedule.daily_cron` (default `"0 1 * * *"`) alongside the
  existing weekly `cron`.
- `launchd.py`: add `render_daily_plist` producing `dev.vnmaster.daily`, which
  runs `digest --daily` under `StartCalendarInterval` with Hour/Minute only
  (omitting `Weekday` makes launchd run it every day). Extend the cron parser
  to accept `*` in the weekday field for the daily expression.
- `install-scheduler` command and the init wizard both render and install the
  third plist and print its bootstrap/bootout commands.

## Migration

One Alembic migration:

- add `digest_runs.kind` (String, default `'weekly'`, backfill existing rows to
  `'weekly'`),
- add `library_games.last_daily_notified_version` and
  `library_games.last_daily_notified_status`, backfilling both to the row's
  current `latest_upstream_version` / `status`.

## Deployment note

Run a normal `vnmaster digest` (weekly) before deploying so the current
backlog goes out through the weekly channel; the migration then baselines the
daily columns to those same versions, so the first nightly run is quiet.
(Already done during design.)

## Out of scope (YAGNI)

- A separate daily audit table (daily runs live in `digest_runs` with
  `kind='daily'`).
- Per-game or per-run mention configuration (always `@everyone` for daily).
- Freshly scanning disk state inside the daily run beyond what the shared
  pipeline already does.

# F95Checker staleness warning

## Problem

F95Checker on macOS goes days without refreshing its library (no auto-refresh
timer exists in foreground mode, App Nap freezes the background timer, and the
Mac sleeps most of the night). VNMaster's digests read F95Checker's db, so a
stalled refresh makes nightly runs silently report "no updates" when the truth
is "nothing was checked". Nobody notices until updates pile up.

## Behavior

At digest time, compare `settings.last_successful_refresh` from F95Checker's
db against the current time. If it is more than 24 hours old, the run is
"stale" and warns:

- Daily mode (`--daily`), no new updates, stale: instead of the current silent
  early return, post a standalone warning message. No `digest_runs` row is
  recorded, matching the existing no-updates path.
- Any mode that posts a digest, stale: append the warning line to the kickoff
  text.
- Not stale, or staleness unknown: behavior unchanged.

Warning text (both places, one line):

    @everyone F95Checker data is stale — last successful refresh was
    3 days ago (Jul 12 09:14). Open F95Checker and hit Refresh.

Age renders as whole hours below 48h ("31 hours ago"), whole days at or above
("3 days ago"). Timestamp uses the local timezone, format `%b %d %H:%M`. The
`@everyone` ping is intentional and repeats nightly while stale; the webhook
adapter in `cli.py` already allows everyone-mentions. The daily kickoff
already begins with `@everyone`, so the warning line only carries its own
`@everyone` when the message it lands in doesn't already have one (standalone
warning, weekly kickoff).

Semantics caveat: F95Checker bumps `last_successful_refresh` at the end of any
full-library refresh pass, even when individual games failed inside it. The
warning therefore means "no refresh pass ran in 24h", nothing finer.

## Components

`src/vnmaster/db/ro_f95checker.py` — new method on `F95CheckerDB`:

    def last_successful_refresh_epoch(self) -> int | None

Reads `SELECT last_successful_refresh FROM settings` (single row). Returns
`None` when the settings table or column is missing (older/newer F95Checker
schemas) or when the value is 0 (F95Checker's default, never refreshed).
`None` means "can't assess" and suppresses the warning; core-schema
verification stays about the `games` table only.

`src/vnmaster/pipeline.py` — module constant
`STALE_REFRESH_SECONDS = 24 * 3600`. `run_digest_pipeline` computes staleness
once after the schema check, builds the warning line via a small helper
(`_stale_warning(last_refresh_epoch, now_epoch) -> str | None`), posts the
standalone message on the stale daily no-updates path, and appends the line to
`kickoff` otherwise.

No config changes, no db schema changes, no new dependencies.

## Testing

Unit tests for the accessor against a temp sqlite file: value present, value
0, column missing, table missing. Pipeline unit tests with injected deps (no
network): fresh timestamp posts nothing extra; stale daily with no updates
posts the standalone warning; stale daily with updates and stale weekly get
the warning appended to kickoff; `None` staleness never warns. Helper test
pins the hours/days wording boundary at 48h.

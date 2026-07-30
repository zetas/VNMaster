# Findings: digest version rendering, save matching, scanner depth

Session date: 2026-07-29. Handoff notes so a later session can pick this up
without re-deriving anything.

Repo state at write time: `main`, two commits added.

- `e06835e` fix(digest): stop gluing a bogus v onto version numbers in embed titles
- `5bea239` fix(scanners): find games and save dirs nested below the root

Both are deployed (`uv tool install . --force`) and the bot was restarted.

---

## 1. Why the digest went silent for a week (fixed, no code change)

Symptom: no Discord output between 2026-07-25 and 2026-07-29. The bot looked
healthy the whole time.

Cause: on 2026-07-24 the repo copy ran and performed the one-time migration in
`load_runtime_settings` that moves `discord.webhook_url` out of `config.toml`
into `secrets.toml`. The installed `uv tool` copy was from 2026-07-15 and still
declared `webhook_url: str` as required, so every scheduled digest died at config
load:

```
vnmaster.config.ConfigError: failed to load ~/.config/vnmaster/config.toml:
discord.webhook_url  Field required
```

Fixed by `uv tool install . --force`.

**Two traps worth remembering.**

1. A healthy bot proves nothing about the digest. The bot is a long-running
   process that read its config at startup and holds it in memory; the digest is
   a separate one-shot launchd job. Check
   `launchctl list | grep vnmaster` for a non-zero exit status on
   `daily`/`weekly`, and read `~/Library/Logs/VNMaster/daily.err.log`. The
   `.out` logs only ever say "digest run complete".
2. Reinstalling can move the interpreter dir (`lib/python3.14` -> `lib/python3.12`),
   which deletes the site-packages an already-running bot is executing from.
   Always `launchctl kickstart -k gui/$(id -u)/dev.vnmaster.bot` after deploying.

---

## 2. Version prefix bug (fixed, `e06835e`)

`digest/embeds.py` prefixed `v` onto version strings unconditionally. Versions
arrive from two sources with different conventions:

- F95Zone thread titles usually carry their own prefix (`v23`, `v0.7.05`) or a
  chapter/season label instead (`Ch.4`, `S2 Ch.18`, `Ep.11`, `Part 7 v0.108`).
- Ren'Py save files hold a bare `config.version` (`0.5.2`, `4.0`).

So the digest rendered `vv23`, `vCh.4`, `vS2 Ch.18`, `vV1.0`. **22 of 52 games
were affected**, not just the one that was noticed.

Fix is `_version_label()`: prefix only when the string starts with a digit. Four
tests cover doubled-v, labelled, bare-numeric and uppercase-V cases.

---

## 3. Scanner depth bug (fixed, `5bea239`)

Neither scanner looked past the root's direct children.

**`scan_disk` found 0 of 3 installed games**, because they sit under
`~/Games/renpy-8.5.0/` rather than directly in `~/Games`. Every `library_games`
row had an empty `install_path`. Now descends a bounded 2 levels and stops at a
recognised game.

**`scan_play_history` missed `Talothral/Sorcerer2`**, a save dir one level down
because that game sets `config.save_directory` to a path. Consequence was real:
Sorcerer 2 (thread 287738) was matched to Sorcerer 1's save folder and reported
version `1.0.0` against upstream `v0.13`, so it looked *ahead* of upstream and
its updates were suppressed. Now:

```
Sorcerer 2   save_dir=Talothral/Sorcerer2   yours=v0.4.0   upstream=v0.13
Sorcerer     save_dir=Sorcerer-1594914541   yours=v1.0.0   upstream=v1.0.0
```

Two details that matter if this code is touched again:

- **Skip `sync/`.** 68 of 70 save folders have one, holding copies of the
  parent's saves. Recursing naively adds 68 phantom games. There is a regression
  test pinning this.
- Nested dirs are keyed by path relative to the root (`Talothral/Sorcerer2`) so
  they stay distinct from a same-named top-level dir. `_clean_for_match` now
  compares the **last path segment**, which lifts the score against "Sorcerer 2"
  from 85.3 to 94.7. At 85.3 it cleared the threshold by 0.3 points.

---

## 4. Pairings added (data, not code)

`vnmaster pair` writes confidence 1.0, which `_persist_learned_pairings` never
clobbers (it gates on `lp.confidence > existing.confidence`). Verified: a run
learns `ForbiddenThoughts-1767723706 -> Forbidden Thoughts` at 0.95, which would
otherwise undo the fix below, and it correctly loses to the manual row.

| thread | save dir | why |
|---|---|---|
| 257669 | `TLC102-1751040621` | The Lusanti Chronicles. Acronym, WRatio 32.7 |
| 133017 | `HIBAH_S2` | How I Became a Hero. Acronym, WRatio 36.0 |
| 70125 | `AltExtBuildBase-1589474498` | Alternate Existence. WRatio 47.1 |
| 90568 | `sixteenyearslater-1627631125` | 16 Years Later!. WRatio 50.0 |
| 281831 | `ForbiddenThoughtsChapter2-12351323` | **Repaired.** Was bound to the older part |

Forbidden Thoughts had been pointing at `ForbiddenThoughts-1767723706`
(`_version 0.3.0`) while the real current progress is
`ForbiddenThoughtsChapter2-12351323` (`_version update3`, matching upstream
`Ch.2 Up.3`). It was reporting as behind when it was current.

Library games without a save dir went from 8 to 4.

**Multi-part games:** when a game ships seasons/parts as separate downloads, the
`pairings` table holds one save dir per thread, and David's rule is to bind the
**latest** part and ignore the rest. Unpaired earlier parts, working as intended:
`HIBAH` (S1), `ThiefofHearts-Saves` and `-Part2-Saves` (P3 is paired),
`FetishLocator-Week2`, `FriendsinNeed-1985`, `TheWorldEclipse-1613963443` (same
project id `1613963443` as the paired folder, so the game was renamed).

---

## 5. Environment facts that are easy to get wrong

**F95Checker is a thread-tracking inbox only.** David adds games via the browser
addon so VNMaster picks them up. He does not launch games through it or track
state there. These columns therefore carry no signal and must never be used as
evidence that a game was or wasn't played (measured across 52 games):

| column | state |
|---|---|
| `executables` | `[]` for all 52 |
| `last_launched` | `0` for all 52 |
| `installed` | empty for 36/52, and empty for 29 of the 44 games that demonstrably do have save folders |
| `finished` | empty for 37/52 |

`added_on` is not a proxy for when he started playing either.

Play evidence comes only from `~/Library/RenPy` save folders and the disk scanner
over `~/Games`.

**`ro_f95checker.py` reads a column that does not exist.** It queries
`executable` (singular); the schema has `executables` (plural, a JSON list).
Because the name sits in `OPTIONAL_COLUMNS`, the absent column silently becomes
NULL rather than erroring, so `F95CheckerGame.executable` is None for every row
and the executable-path branch in `_resolve_disk` has never executed.

**`save_dir_hint` is usually None.** It is parsed from `game/options.rpy`, plain
source that shipped Ren'Py builds do not include (only the SDK's own sample
projects have it). It did populate for `ForbiddenThoughts-0.3.0-pc`, so it is not
always empty, just unreliable.

---

## 6. Evaluated and rejected: project timestamp as a matching signal

The `-1627631125` suffix on a save folder is a Unix timestamp of Ren'Py project
creation. Idea was to compare it against release date to narrow weak matches.
Tested by calibrating `thread_id -> project-date` with piecewise-linear
interpolation over the 32 confirmed timestamped pairs (thread ids are monotonic
in time, so they proxy release date; there is no first-release column locally).

**Do not re-litigate this without new information. It does not work as an
automatic signal.**

- **As a positive identifier: fails.** Of 3 known answers only
  `TLC102 -> Lusanti` ranked #1; `AltExt` #2, `sixteenyearslater` #3. Worse,
  already-correctly-paired folders produce confident wrong top-1 hits with
  *smaller* gaps than the true answers carry: `Tuneintotheshow -> HIBAH` 0.07y,
  `Heaven2 -> BJoM` 0.13y, `FetishLocator -> WVM` 0.15y. No threshold separates
  them.
- **Why:** leave-one-out error is median 0.57y, p90 1.66y, max 4.62y, far wider
  than the time spacing between candidate games. 52 games over ~7 years.
- **As an automatic veto: unsafe.** Rejects 20% of true pairs at a 1y threshold,
  13% at 1.5y, 7% at 2y; only 5y+ is lossless, and by then it vetoes nothing.
  It would break two currently-correct pairings: **Sorcerer 2** (gap 5.48y,
  sequels reuse the prequel's save dir) and **Prince of Suburbia Rewrite**
  (2.35y, rebuilt project on an old thread). Both directions have real
  counterexamples, so neither "project long before release" nor "long after" is
  impossible.
- **Coverage:** 11 of 43 paired folders have no timestamp at all, including the
  worst offenders (`HIBAH`, `SSP`, `DR2`).
- **Verdict:** worth *displaying* as a hint in `suggest-pairs` for human review.
  Never an auto-pair or auto-reject gate.

---

## 7. Open items

Roughly highest value first.

### 7a. `installed_version` from disk overrides the save version unconditionally

`matcher.py` ~line 174, commented "disk version is authoritative when present".
This is wrong when the on-disk install is older than the saves, which is the
case for Forbidden Thoughts (install `0.3.0`, saves `update3`). Right now it is
masked only because that folder fails to fuzzy-match, so **fixing 7b first would
regress Forbidden Thoughts**. Needs a decision: prefer whichever is newer, or
prefer the save version when they disagree. This is a judgment call about how
David's setup works, so ask rather than guess.

### 7b. Disk folder names miss the fuzzy threshold

The fix in section 3 finds 3 installs but only 1 matches:

```
My Demonic Romance           -> 'My Demonic Romance' @ 100.0   matched
ForbiddenThoughts-0.3.0-pc   -> 'Forbidden Thoughts' @  77.3   missed
LHiH-v1.1b-pc                -> 'Sorcerer'           @  20.0   correctly missed, not in library
```

`_NOISE_SUFFIXES` does not strip a platform suffix (`-pc`, `-mac`, `-linux`) or a
mid-string version (`-0.3.0-pc`). Straightforward to extend, but see 7a.

### 7c. Baseline resolution breaks when version schemes disagree

Two of the newly paired games will show inflated star ratings, because the save's
`config.version` shares no numbering with the changelog labels:

| game | yours | changelog labels | result |
|---|---|---|---|
| The Lusanti Chronicles | `1.04b` | `v1.05`, `v1.04b`, ... | exact, counts 1/8 blocks, ~2.6h. Correct |
| Alternate Existence | `2.6.0_ip` | `Season 2 - 2.6.0`, ... | token `2.6.0` matches. Correct |
| How I Became a Hero | `0.17` | `Season 2 - Chapter 18` | unplaceable, counts all blocks |
| 16 Years Later! | `day19-20.` | `Ep.16 Full`, `Ep.15` | unplaceable, counts all blocks |

`resolve_baseline` falls back to counting the whole history and tags itself
`confidence=low`, `basis='versions unreadable'`, so the Accuracy column reads
"rough" rather than lying. The label is honest but the rating overstates added
content. Needs a way to anchor a dev-internal version against a marketed one.

Note the general lesson: a game's marketed version scheme and its internal
structure can disagree. 16 Years Later! ships "Ep.16" but names its scripts
`dayNNscript.rpy`.

### 7d. Acronym save dirs cannot be matched automatically

Now handled by manual pairings, but the underlying gap stands: an acronym scores
far below even the 70 corroboration floor, so version corroboration is never
attempted. `HIBAH` -> How I Became a Hero is 36.0. Corroboration would not
rescue these anyway: tokens differ by patch (`2.6.0` vs `2.6.2`, `1.04` vs
`1.05`) or the upstream string has no dotted numeric token at all (`Ep.16 Full`,
`S2 Ch.18`; the regex needs digit.digit, so `Ep.16` does not qualify). Would need
an initialism scorer.

### 7e. `pair` rejects the URL F95Checker itself stores

```
$ vnmaster pair TLC102-1751040621 https://f95zone.to/threads/257669
Error: 'https://f95zone.to/threads/257669' does not look like an F95 thread URL
```

`_F95_THREAD_RE` is `f95zone\.to/threads/[^/]*\.(\d+)/?`, requiring the slug form
`threads/<slug>.<id>/`. Any slug works since only the trailing id is parsed, so
`https://f95zone.to/threads/.257669/` is enough. Accepting the bare form too
would remove a papercut, since that is the form F95Checker and the addon produce.

### 7f. `suggest-pairs` timestamp hint

Display the project date and nearest-release candidate as review context, per
section 6. Cheap, safe, no gating.

### 7g. Four library games still have no save folder

```
233041  By Justice or Mercy     v23
 43072  My Employee's Family    Ep.8b Bugfix
 35910  WVM                     S2 Ch.1 Ep.13 F
 51349  What a Legend!          v0.7.05
```

By Justice or Mercy was checked thoroughly and is genuinely absent: no save
folder, no install, and no unpaired folder is both new enough (created after its
2024-11-14 first release) and day-structured at ~20 days. `sixteenyearslater`
matched on structure (`day19-20.`, days 9/18/19 vs its Day 20 / day 19 / Day 18)
but its 2021 project timestamp and folder name rule it out. The other three were
not investigated in depth.

---

## 8. Diagnostic recipes

**Identify an unknown save dir**, in order of strength:

1. **Project timestamp in the folder name** (`Foo-1627631125` -> 2021-07-30).
   A folder cannot belong to a game whose first release postdates it. Strongest
   single discriminator, but see section 6 for its limits.
2. **Script filenames in the save's `log` entry** (`day19script.rpy`) reveal
   content structure, matchable against the F95 changelog.
3. **`_version` from the `json` entry** (the game's `config.version`).
4. **`screenshot.png`** for a visual check.

A `.save` is a zip containing `screenshot.png`, `extra_info`, `json`,
`renpy_version`, `log`, `signatures`.

Do **not** grep save bytes for title words. Entries are deflate-compressed, and
short needles like "jom" hit dialogue in ~25 unrelated games.

**Useful paths**

- config: `~/.config/vnmaster/config.toml`, `~/.config/vnmaster/secrets.toml`
- db: `~/Library/Application Support/vnmaster/vnmaster.db`
- F95Checker db (read-only source): `~/Library/Application Support/f95checker/db.sqlite3`
- logs: `~/Library/Logs/VNMaster/{bot,daily,weekly}.err.log`
- launchd: `dev.vnmaster.bot` (persistent), `dev.vnmaster.daily`, `dev.vnmaster.weekly`
- deploy: `uv tool install . --force`, then kickstart the bot

**`vnmaster digest --force` posts to Discord.** There is no dry-run. To inspect
matching without posting, call `scan_play_history` / `scan_disk` / `match_library`
directly with `_load_pairings`, as done throughout this session.

**Prompt changes need a cache clear.** LLM changelog extractions are cached in
`changelog_extractions`, keyed on `f95_thread_id` + a sha256 of the raw
changelog. A changed prompt does not re-extract existing games; only a changed
changelog does. Force with
`DELETE FROM changelog_extractions [WHERE f95_thread_id IN (...)]`.

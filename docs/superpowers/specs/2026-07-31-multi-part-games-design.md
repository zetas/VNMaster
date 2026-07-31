# Multi-part game downloads

## Problem

Some devs split one story into several discrete games so no single download gets
too big (Grandma's House is 7 separate ~20GB games in one thread). Today the
selector treats every link in the thread as a mirror of one artifact:
`_select_game_artifact` takes the first eligible link as the game and demotes
the rest to fallback mirrors. The user gets Part 1 installed and never learns
Parts 2-7 exist.

## Goal

`vnmaster fetch` recognizes a multi-part thread and lets the user pick one or
more parts to download. Each part is a complete standalone game, so each gets
its own download, extraction, add-ons, URM, and verification. The library
still sees one game.

## Detection

Lives in `selector.py`, next to the other group-name heuristics. Runs on
thread metadata before any artifact selection.

- Scan download group names that pass `_REJECT_GAME_GROUP_RE` and do NOT match
  `_OPTIONAL_GROUP_RE` ("Part 2 walkthrough" is add-on material, not a part).
  Note: game selection today does not exclude optional groups; detection must
  apply both filters itself.
- Token families, with aliases folded together: part/pt, chapter/ch,
  episode/ep, volume/vol, season, book, act. Pattern: family token followed by
  a number, separators space/dot/dash/`#` allowed.
- A thread is multi-part only when one family appears with two or more
  distinct numbers. A lone "Season 2" never triggers.
- If several families qualify, pick the one with the most distinct numbers.
  On a tie, priority order: part > chapter > episode > volume > book > act >
  season.
- If two qualifying families co-occur inside the same group names ("Season 1
  Episode 2"), the numbering is composite and one family cannot identify a
  game. Treat the thread as NOT multi-part, warn the user, and fall back to
  today's behavior.
- A group joins a part only if it contains exactly one number of the chosen
  family. Groups with several ("Part 1-2 bundle") are ignored for parts, with
  a warning.
- Game-eligible groups with no part token in a detected multi-part thread are
  ignored for game selection (ambiguous). They stay eligible for the embedded
  add-on pass.
- If none of a part's groups match a configured platform (bare "Part 1"
  headings), the part's groups are treated as platform-neutral and remain
  eligible with `platform=None`, like add-on groups today.
- HTML-fallback threads are undetectable: `_scrape_thread_download_groups`
  collapses all links into one "Thread download links" group. When metadata
  comes from the fallback, print a warning that multi-part detection is
  unavailable and proceed as today.
- Threads with no detection follow the current single-artifact path unchanged.

## CLI flow (two-phase)

Part selection happens between thread-info fetch and plan building, on a
detected-part inventory (label, group names, platform/host summary), not on a
built plan:

1. Fetch thread info, run detection.
2. Multi-part: resolve the selection (prompt or `--parts`).
3. Build the plan with only the selected parts.
4. Existing flow continues: optional add-on picker, confirmation, execution.

Selection rules:

- Interactive: questionary checkbox (same widget as the add-on picker). One
  row per part: label, platform/host summary, "(installed v1.2)" marker read
  from install state for this thread. Nothing pre-checked. Empty selection
  aborts with a clear message. Non-TTY fallback mirrors
  `_prompt_optional_selection_fallback`.
- `--parts` accepts comma-separated single numbers and ranges ("1,3-5") or
  "all". Values are detected part numbers, not list positions. A single
  number that does not exist is an error. A range contributes its
  intersection with the detected numbers ("1-5" on parts 1, 2, 4 gives
  1, 2, 4; "8-9" contributes nothing). If the whole selection resolves empty,
  error.
- `--yes` on a multi-part thread without `--parts` is an error telling the
  user to pass `--parts`. No implicit 100GB downloads.
- `--parts` on a thread with no detected parts is an error.
- `--dry-run` without `--parts` prints the detected part inventory and exits.
  With `--parts` it prints the resulting plan as today.

## Plan model

- `PlannedArtifact` gains `part: str | None` (display label, e.g. "Part 3").
  `kind` stays `"game"`.
- `_select_game_artifact` becomes `_select_game_artifacts`, returning one
  artifact per selected part (each with its own primary mirror and
  alternates, chosen by existing host preference within that part's groups),
  or a single-element list for normal threads.
- The required/optional split becomes kind-based, not positional:
  `select_optional_artifacts` (`workflow.py`) and the CLI currently assume
  `artifacts[0]` is the only required item. Both must partition by
  `kind == "game"` (all games required) vs `"addon"` (optional). Otherwise
  parts 2-7 would show up in the add-on picker as removable.
- Add-on targeting: an add-on whose group or title carries the chosen family
  token with a part number attaches to that part. Untagged installable
  add-ons apply to every selected part (preview and merge per part). URM
  installs into every selected part.

## Execution: per-part transactions

The current service is one transaction: refuse an existing version dir, one
`staging/game`, publish everything with a single rename, delete staging on any
failure. That cannot express "add Part 3 later" or "keep Parts 1-2 when Part 4
fails", and two game artifacts would collide in `staging/game`. So for parts,
the transaction boundary moves down one level:

- Layout: `~/Games/<Game>/<version>/<part>/`, where each part dir has the
  same internal structure a version dir has today (`game/`, `archive/`,
  `addons/`). Single-part threads keep today's layout and today's whole-plan
  transaction, unchanged.
- Each part executes in its own staging dir and publishes atomically into
  `<version>/<part>/` as it completes. The version root may already exist
  when parts are involved; a part dir that already exists is only replaced
  when the user selected that part (re-download), via stage-then-swap with
  the old part dir removed after the new one lands.
- Add-ons targeted at a part are downloaded and merged inside that part's
  transaction. Add-ons applying to all parts are downloaded once, then merged
  into each part; their payload is preserved in each part's `archive/`.
- Verification runs per part on that part's `game/`.
- Parts run sequentially. A part that fails all mirrors is reported and does
  NOT roll back or halt others: remaining parts still run. The command exits
  nonzero listing which parts succeeded and which failed.
- State is saved after each successful part, not once at the end, so an
  interrupt cannot orphan completed parts.

## Install state

`save_install_state` today assumes `artifacts[0]` is the game and rewrites the
whole row payload on every save. It needs merge semantics:

- One row per version dir, as today. Part key = the normalized part label.
- `_artifact_payload` persists the new `part` field.
- On save after fetching parts, entries (artifacts, archive hashes,
  verification checks) belonging to the parts just fetched replace their
  previous versions; entries for other parts are kept. Legacy single-game
  saves keep full-replace behavior.
- Row-level platform/host reflect the most recent fetch; authoritative
  per-part platform/host/source live in the artifact entries.
- Per-part Ren'Py dir and URM path are recorded per artifact entry; the
  row-level columns hold the first part's values for legacy compatibility.

## Rebuild

`rebuild_install` is structurally single-game (requires `<install>/game`,
picks the first game artifact, swaps one dir). For multi-part installs it
loops: each recorded part rebuilds independently with the existing
single-game logic scoped to `<version>/<part>/` (its own staging, backup
under `<part>/backups/`, verification). Part rebuilds are not atomic as a
set; a failure stops the loop and reports which parts were rebuilt. Legacy
installs without parts rebuild exactly as today.

## Scanner and library

- `scan_disk` walks `max_depth=2` from the games root; part dirs sit one
  level deeper than today's install layout, so the depth bound must grow by
  one (bounded, same early-stop at recognized games).
- The matcher currently keeps one installed game per thread; multiple part
  dirs resolving to the same thread must aggregate into a single library
  entry (version root path, parts list, summed size) instead of last-one-wins.
- Digest and save matching operate on the aggregated entry; no per-part rows
  in the digest.

## Testing

- Detection positives: part, chapter, episode variants; alias folding
  (pt/part); mixed platforms per part; platform-neutral part headings.
- Detection negatives: lone "Season 2", version strings, "Update Ch 5"
  rejected, "Part 2 walkthrough" excluded, composite Season+Episode threads
  fall back with a warning, HTML-fallback threads warn and fall back.
- `--parts` parsing: single numbers, lists, ranges, "all", junk, nonexistent
  single number errors, empty resolution errors, range intersection with
  gapped part numbers.
- `--yes` without `--parts` on a multi-part thread errors; `--parts` on a
  normal thread errors; `--dry-run` inventory listing.
- Plan building: one artifact per selected part, kind-based required/optional
  partition (parts never appear in the add-on picker), part-tagged add-on
  attaches to its part, untagged add-on applies to all selected parts.
- Execution: two parts publish to sibling dirs; second-part failure keeps and
  records the first; re-fetch replaces only the chosen part; add-part run
  with an existing version root; interrupt after part 1 leaves part 1
  recorded.
- State: merge keeps sibling part entries; legacy single-game save unchanged.
- Rebuild: two-part install rebuilds both; failure mid-loop reports progress;
  legacy install rebuilds as today.
- Scanner/matcher: parts found at the deeper level; multiple parts aggregate
  to one library entry; single-game scanning regression-free.
- Regression: single-part threads produce byte-identical plans and layout.

## Decisions made during design

- One library entry with part subdirs, not separate installs per part.
- Checkbox defaults to nothing selected, even on first fetch.
- Refetch shows all parts with installed markers, nothing pre-checked.
- Approach A (multi-artifact plan) over per-part pipeline loops or parse-time
  detection in `f95.py`.
- After external review: per-part transaction boundary with part dirs
  mirroring the version-dir structure; kind-based required/optional
  partition; state merge semantics; part-by-part rebuild; composite
  numbering and HTML-fallback threads fall back to single-game with a
  warning.
- After final review: add-on targeting (line 98) narrowed at implementation
  time to embedded download groups only. A separate add-on thread whose
  title carries the family token is not tagged to a part; it stays shared
  across every selected part like an untagged add-on.

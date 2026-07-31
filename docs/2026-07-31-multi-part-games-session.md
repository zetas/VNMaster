# 2026-07-31 session: multi-part game downloads

Context for future sessions and reviewers. Everything here is merged to main.
Range: 04655ac..37b2462 (spec, plan, 20 feature commits, 1 post-merge fix).

## What was built

Threads like Grandma's House (#94140) split one story into several discrete
games so no single download gets too big. Before this session, `vnmaster
fetch` treated every link in such a thread as a mirror of one artifact: it
installed Part 1 and silently used Parts 2-7 as fallback mirrors.

Now:

- `detect_parts` (selector.py) scans download group names for token families
  (part/pt, chapter/ch, episode/ep, volume/vol, book, act, season). A thread
  is multi-part only when one family shows 2+ distinct numbers. Ties break by
  the priority order above. Composite numbering ("Season 1 Episode 2") and
  hand-scraped fallback threads fall back to single-game with a warning.
- Fetch shows a checkbox of parts (nothing pre-checked, "(installed vX)"
  markers) or takes `--parts 1,3-5` / `--parts all`. `--yes` without
  `--parts` on a multi-part thread is an error. `--dry-run` without `--parts`
  prints the part inventory and exits.
- The plan carries one `kind="game"` artifact per selected part
  (`PlannedArtifact.part` holds the label, e.g. "Part 3"). The
  required/optional split is kind-based everywhere; game parts never appear
  in the add-on picker.
- Execution: each part is its own transaction, staged and published
  atomically into `~/Games/<Game>/<version>/<Part N>/` (same internal layout
  a version dir has: game/, archive/, addons/). A failed part does not roll
  back or halt the others. State saves after each successful part.
  Single-part threads keep the old whole-plan transaction, byte-identical.
- Install state merges per part (paths and hash keys prefixed "Part N/",
  verification lines prefixed "Part N: "). Rebuild loops parts with the old
  single-game logic scoped per part dir. Legacy rows are untouched.
- Scanner default depth went 2 -> 5 so part-nested builds are found. Side
  effect worth knowing: depth 2 never reached vnmaster's own single-game
  installs at `<Game>/<version>/game/<BuildDir>`, so those become
  discoverable for the first time too.
- Matcher aggregates multiple part installs of one thread into one library
  entry (install_path = version root, sizes summed) keyed on a "Part N"
  path component, not parent equality. Leftover version dirs side by side do
  not aggregate (last-one-wins as before).

Docs: spec at docs/superpowers/specs/2026-07-31-multi-part-games-design.md,
plan at docs/superpowers/plans/2026-07-31-multi-part-games.md, README has a
"Multi-part games" section.

## Post-merge bug and fix (37b2462)

First real run failed: parts detected, two picked, then "No selected part of
'Grandma's House' was downloadable".

Root cause: the design assumed part tokens live in the mirror groups
("Part 1 - Win"). The real thread publishes each part as an empty heading
group ("Part 7", zero mirrors) followed by plain "Win/Linux"/"Mac" groups
holding the links with no part token. Each detected part owned only its empty
heading; the spec rule "tokenless groups are ambiguous, ignore them" threw
away every real mirror group.

Fix: ownership is positional. A part heading captures the mirror-bearing
groups after it until any other heading ends the section: a mirror-less group
("Update Patch (v0.107>v0.108)", the "Part 1 - Part 6" divider, footnotes) or
any other tokened group. On the real thread this attaches Win/Linux + Mac to
each part and correctly leaves the update patch's own Win/Mac groups
unattached. Verified against the live thread and with
`fetch "grandma's house" --dry-run --parts 1,7`.

Detection input for that thread, for reference: 53 groups, parts as empty
headings at indexes 0, 9, 14, 19, 24, 29, 34, platform groups following each,
plus walkthrough PDF attachments, a "Patches" group, and two footnote groups.

## Review findings fixed before merge

- Positional required/optional assumptions (`artifacts[0]` is the game) in
  workflow, CLI printing, and the picker all replaced with kind-based
  partitions.
- Embedded add-ons tagged to unselected parts are dropped at plan build so
  the picker never offers something execution would silently skip.
- 3+ part matcher aggregation chains correctly (a merged entry's path is the
  version root, which itself has no part component).
- Strict mypy narrowing for part labels in the service loop.
- Rebuild prints the version root for multi-part installs.

## Known deferred items (also in auto-memory multi-part-games-followups)

- `_guard_incompatible_addons` (cli.py) still slices `plan.artifacts[1:]`
  positionally. Inert today: game artifacts never carry `warning`. If that
  ever changes, a required part could be prompted away as an incompatible
  add-on. Fix is to filter by kind == "addon".
- `execute_multipart_plan` creates the version root before any part
  succeeds; an all-parts-failed fetch leaves an empty `<Game>/<version>/`.
- Pre-feature install sitting in a version root that later gets parts is a
  chimera: legacy entries survive merges, rebuild ignores them.
- `_installed_part_versions` picks an arbitrary row when a part exists in
  several recorded versions.
- Row-level renpy_game_dir/urm_path only set on row creation; can go stale
  vs per-part artifact entries.
- Matcher accepted tradeoff: an old non-split install located exactly at a
  later split release's version root aggregates with the new parts.
- Update-patch sections in multi-part threads (mirror groups under an
  "Update Patch" heading) are excluded from parts but are not offered as
  add-ons either, because their group names ("Win/Linux") do not match the
  optional pattern. Same class as before the feature, now visible.

## Test state at 37b2462

489 passed, 1 skipped (pre-existing), mypy clean (63 files), ruff clean.

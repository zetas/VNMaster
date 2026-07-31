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
its own download, extraction, and verification. The library still sees one
game.

## Detection

Lives in `selector.py`, next to the other group-name heuristics.

- Scan download group names that already pass `_REJECT_GAME_GROUP_RE`.
- Token pattern: `part|pt|chapter|ch|episode|ep|season|book|act|vol|volume`
  followed by a number (allowing separators like space, dot, dash, `#`).
- A thread is multi-part only when the same token family appears with two or
  more distinct numbers ("Part 1 - Win" plus "Part 2 - Win"). A lone
  "Season 2" never triggers detection.
- If two families both qualify, the one with more distinct numbers wins.
- Each part gets a normalized display label ("Part 3") and owns the groups
  that mention its number. Platform matching and host preference run inside a
  part's groups exactly as they do today for a whole thread.
- In a detected multi-part thread, game-eligible groups with no part token are
  ignored for game selection (they are ambiguous). They stay eligible for the
  embedded add-on pass if they match the optional-group pattern.
- Threads with no detection follow the current single-artifact path unchanged.

## Selection UX

- Interactive: after plan discovery, a questionary checkbox (same widget as
  the add-on picker at `cli.py:971`) lists parts in order. Each row shows the
  part label, a platform/host summary, and an "(installed v1.2)" marker read
  from install state. Nothing is pre-checked. Empty selection aborts with a
  clear message. Non-TTY fallback mirrors `_prompt_optional_selection_fallback`.
- New `--parts` flag: accepts comma lists and ranges ("1,3-5") or "all".
  Values refer to the detected part numbers, not list positions. A range
  selects its intersection with the detected numbers, so "1-5" on a thread
  with parts 1, 2, 4 selects 1, 2, 4. Naming a number that does not exist is
  an error.
- `--yes` on a multi-part thread without `--parts` is an error that tells the
  user to pass `--parts`. This is deliberate: no implicit 100GB downloads.
- `--parts` on a thread with no detected parts is an error.
- Re-selecting an installed part re-downloads and replaces it. Selecting a new
  part adds it alongside the existing ones.

## Plan model

- `PlannedArtifact` gains one optional field: `part: str | None` (display
  label). `kind` stays `"game"`.
- `_select_game_artifact` becomes `_select_game_artifacts`, returning a list:
  one artifact per selected part (each with its own primary mirror and
  alternates), or a single-element list for normal threads.
- Embedded add-on selection is untouched. `_OPTIONAL_GROUP_RE` classification
  runs before part handling, so "Part 2 walkthrough" stays an add-on.

## Install layout and state

- Artifacts with a `part` install into `~/Games/<Game>/<version>/<part>/`
  instead of the version root. One library entry per thread, so digest and
  save matching are unaffected (scanners already handle nested game dirs).
- Install state records the list of installed part labels. Preserved payloads
  are kept per part; `vnmaster rebuild` re-extracts each part into its own
  subdir.
- Re-fetching one part replaces only that part's subdir.
- If the thread version bumps and the user fetches a new part, it lands in the
  new version dir. Older parts stay in the old version dir until the user
  explicitly re-downloads them. Every part download is explicit.

## Errors and verification

- A selected part with no compatible platform/host mirrors becomes a skip
  entry with a reason; the plan still shows it. If every selected part is
  unselectable, raise the existing `NoCompatibleDownloadError`.
- Parts download sequentially. If one part fails all its mirrors, parts that
  already installed stay installed and recorded in state. The run exits
  nonzero and names which parts succeeded and which failed.
- Verification runs per part dir.

## Testing

- Detection positives: part, chapter, episode variants across mixed platform
  groups.
- Detection negatives: lone "Season 2", version strings, "Update Ch 5" still
  rejected by the update filter.
- `--parts` parsing: single numbers, comma lists, ranges, "all", junk input.
- Plan building emits one artifact per selected part with correct mirrors.
- `--yes` without `--parts` on a multi-part thread errors.
- State records installed parts; re-fetch replaces only the chosen part.
- Rebuild restores a two-part install from preserved payloads.
- Regression: single-part threads produce identical plans to today.

## Decisions made during design

- One library entry with part subdirs, not separate installs per part.
- Checkbox defaults to nothing selected, even on first fetch.
- Refetch shows all parts with installed markers, nothing pre-checked.
- Approach A (multi-artifact plan) over per-part pipeline loops or parse-time
  detection in `f95.py`.

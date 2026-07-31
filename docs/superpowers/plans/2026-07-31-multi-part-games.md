# Multi-Part Game Downloads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `vnmaster fetch` detects threads whose story is split into several discrete games ("Part 1".."Part 7" download groups), lets the user pick parts, and downloads each selected part as its own standalone install under one library entry.

**Architecture:** Detection and part selection live in `selector.py` next to the other group-name heuristics. The plan carries one `kind="game"` artifact per selected part (new `part` label field). Execution moves the transaction boundary down one level for parts: each part stages and publishes atomically into `<version>/<part>/`, which internally mirrors today's version-dir layout, so state, rebuild, and verification reuse the single-game logic scoped per part.

**Tech Stack:** Python 3.12, click, questionary, SQLAlchemy, pytest. Spec: `docs/superpowers/specs/2026-07-31-multi-part-games-design.md`.

## Global Constraints

- Conventional commits: `type(scope): subject`, lowercase, imperative. No attribution of any kind (no Co-Authored-By, no AI mentions).
- No em dashes in commits, comments, or docs. Plain language.
- Single-part threads must behave byte-identically to today: same plans, same layout, same transaction.
- Comments only for constraints code can't show; match existing comment density.
- Run tests with `python -m pytest tests/unit/<file> -q` from the repo root.
- Token families and priority order (spec): part/pt > chapter/ch > episode/ep > volume/vol > book > act > season. Multi-part requires >= 2 distinct numbers in ONE family.
- `--yes` without `--parts` on a multi-part thread is an error. Nothing is ever pre-selected.

---

### Task 1: Part detection

**Files:**
- Modify: `src/vnmaster/downloads/models.py` (add `DetectedPart`, `PartDetection`)
- Modify: `src/vnmaster/downloads/selector.py` (add `detect_parts`)
- Test: `tests/unit/test_download_selector.py`

**Interfaces:**
- Consumes: `DownloadGroup` from `models.py`, `_REJECT_GAME_GROUP_RE` / `_OPTIONAL_GROUP_RE` already in `selector.py`.
- Produces:
  - `models.DetectedPart(number: int, label: str, group_indexes: tuple[int, ...])` (frozen dataclass)
  - `models.PartDetection(family: str | None, parts: tuple[DetectedPart, ...], warnings: tuple[str, ...] = ())` with property `is_multipart -> bool` (true when `len(parts) >= 2`)
  - `selector.detect_parts(groups: tuple[DownloadGroup, ...]) -> PartDetection`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_download_selector.py` (reuse the existing `_thread` / `_group` helpers):

```python
from vnmaster.downloads.selector import detect_parts


def test_detects_parts_from_numbered_groups() -> None:
    groups = (
        _group("Part 1 - Win"), _group("Part 1 - Mac"),
        _group("Part 2 - Win"), _group("Part 2 - Mac"),
    )
    detection = detect_parts(groups)
    assert detection.is_multipart
    assert detection.family == "part"
    assert [p.number for p in detection.parts] == [1, 2]
    assert detection.parts[0].label == "Part 1"
    assert detection.parts[0].group_indexes == (0, 1)


def test_alias_pt_folds_into_part_family() -> None:
    detection = detect_parts((_group("Pt. 1 Win"), _group("Pt. 2 Win")))
    assert detection.family == "part"
    assert detection.parts[1].label == "Part 2"


def test_lone_season_number_is_not_multipart() -> None:
    detection = detect_parts((_group("Season 2 - Win"), _group("Season 2 - Mac")))
    assert not detection.is_multipart
    assert detection.parts == ()


def test_version_numbers_do_not_trigger_detection() -> None:
    detection = detect_parts((_group("Win v1.2"), _group("Mac v1.2")))
    assert not detection.is_multipart


def test_update_groups_and_optional_groups_are_excluded() -> None:
    groups = (
        _group("Ch. 5 Update"),          # rejected by the update filter
        _group("Part 2 walkthrough"),    # optional group, add-on material
        _group("Part 1 - Win"), _group("Part 2 - Win"),
    )
    detection = detect_parts(groups)
    assert detection.family == "part"
    assert all(0 not in p.group_indexes and 1 not in p.group_indexes
               for p in detection.parts)


def test_family_with_more_numbers_wins() -> None:
    groups = (
        _group("Chapter 1"), _group("Chapter 2"), _group("Chapter 3"),
        _group("Book 1"), _group("Book 2"),
    )
    assert detect_parts(groups).family == "chapter"


def test_tie_breaks_by_priority_order() -> None:
    groups = (
        _group("Act 1"), _group("Act 2"),
        _group("Episode 1"), _group("Episode 2"),
    )
    assert detect_parts(groups).family == "episode"


def test_composite_numbering_falls_back_with_warning() -> None:
    groups = (
        _group("Season 1 Episode 1"), _group("Season 1 Episode 2"),
        _group("Season 2 Episode 1"), _group("Season 2 Episode 2"),
    )
    detection = detect_parts(groups)
    assert not detection.is_multipart
    assert detection.warnings and "composite" in detection.warnings[0]


def test_range_numbered_group_is_ignored_with_warning() -> None:
    groups = (_group("Part 1"), _group("Part 2"), _group("Part 1-2 bundle"))
    detection = detect_parts(groups)
    assert detection.is_multipart
    owned = {i for p in detection.parts for i in p.group_indexes}
    assert 2 not in owned
    assert any("Part 1-2 bundle" in w for w in detection.warnings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_download_selector.py -q -k "detect or composite or tie or alias or lone or range"`
Expected: FAIL with `ImportError: cannot import name 'detect_parts'`

- [ ] **Step 3: Add the models**

In `src/vnmaster/downloads/models.py`, after `DownloadGroup`:

```python
@dataclass(frozen=True)
class DetectedPart:
    number: int
    label: str
    group_indexes: tuple[int, ...]


@dataclass(frozen=True)
class PartDetection:
    family: str | None
    parts: tuple[DetectedPart, ...]
    warnings: tuple[str, ...] = ()

    @property
    def is_multipart(self) -> bool:
        return len(self.parts) >= 2
```

- [ ] **Step 4: Implement detect_parts in selector.py**

Add imports (`DetectedPart`, `PartDetection`) to the existing `models` import block, then:

```python
# Priority order for ties; aliases fold into one family.
_PART_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("part", ("part", "pt")),
    ("chapter", ("chapter", "ch")),
    ("episode", ("episode", "ep")),
    ("volume", ("volume", "vol")),
    ("book", ("book",)),
    ("act", ("act",)),
    ("season", ("season",)),
)
_FAMILY_RES = {
    family: re.compile(
        r"\b(?:" + "|".join(aliases) + r")[\s.\-#]*(\d{1,3}(?:\s*-\s*\d{1,3})?)\b",
        re.I,
    )
    for family, aliases in _PART_FAMILIES
}


def detect_parts(groups: tuple[DownloadGroup, ...]) -> PartDetection:
    eligible = [
        (index, group)
        for index, group in enumerate(groups)
        if not _REJECT_GAME_GROUP_RE.search(group.name)
        and not _OPTIONAL_GROUP_RE.search(group.name)
    ]
    owned_by_family: dict[str, dict[int, list[int]]] = {}
    ranged_by_family: dict[str, list[str]] = {}
    for family, _aliases in _PART_FAMILIES:
        owned: dict[int, list[int]] = {}
        ranged: list[str] = []
        for index, group in eligible:
            found = _FAMILY_RES[family].findall(group.name)
            if not found:
                continue
            if len(set(found)) > 1 or any("-" in value for value in found):
                ranged.append(group.name)
                continue
            owned.setdefault(int(found[0]), []).append(index)
        owned_by_family[family] = owned
        ranged_by_family[family] = ranged

    qualifying = [f for f, _ in _PART_FAMILIES if len(owned_by_family[f]) >= 2]
    if not qualifying:
        return PartDetection(family=None, parts=())
    if len(qualifying) >= 2:
        for _index, group in eligible:
            hits = sum(
                1 for f in qualifying if _FAMILY_RES[f].search(group.name)
            )
            if hits >= 2:
                return PartDetection(
                    family=None,
                    parts=(),
                    warnings=(
                        "Download groups use composite numbering "
                        f"({' and '.join(qualifying)}); treating this thread "
                        "as a single game.",
                    ),
                )
    family = max(qualifying, key=lambda f: len(owned_by_family[f]))
    warnings = tuple(
        f"Ignored group with a number range for parts: {name!r}"
        for name in ranged_by_family[family]
    )
    parts = tuple(
        DetectedPart(
            number=number,
            label=f"{family.capitalize()} {number}",
            group_indexes=tuple(sorted(indexes)),
        )
        for number, indexes in sorted(owned_by_family[family].items())
    )
    return PartDetection(family=family, parts=parts, warnings=warnings)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_download_selector.py -q`
Expected: all PASS (including the pre-existing selector tests)

- [ ] **Step 6: Commit**

```bash
git add src/vnmaster/downloads/models.py src/vnmaster/downloads/selector.py tests/unit/test_download_selector.py
git commit -m "feat(downloads): detect multi-part threads from download group names"
```

---

### Task 2: --parts value parsing

**Files:**
- Modify: `src/vnmaster/downloads/selector.py` (add `parse_parts_option`)
- Test: `tests/unit/test_download_selector.py`

**Interfaces:**
- Produces: `selector.parse_parts_option(raw: str, available: tuple[int, ...]) -> tuple[int, ...]`. Raises `ValueError` with a user-readable message on bad input. "all" selects everything. Explicit single numbers must exist; ranges contribute their intersection; a selection that resolves empty is an error.

- [ ] **Step 1: Write the failing tests**

```python
from vnmaster.downloads.selector import parse_parts_option


def test_parse_parts_numbers_ranges_and_all() -> None:
    available = (1, 2, 4)
    assert parse_parts_option("all", available) == (1, 2, 4)
    assert parse_parts_option("1,4", available) == (1, 4)
    assert parse_parts_option("1-5", available) == (1, 2, 4)
    assert parse_parts_option("4,1-2", available) == (1, 2, 4)


def test_parse_parts_rejects_bad_input() -> None:
    available = (1, 2, 4)
    with pytest.raises(ValueError):
        parse_parts_option("3", available)       # explicit number must exist
    with pytest.raises(ValueError):
        parse_parts_option("8-9", available)     # resolves empty
    with pytest.raises(ValueError):
        parse_parts_option("5-1", available)     # backwards range
    with pytest.raises(ValueError):
        parse_parts_option("junk", available)
    with pytest.raises(ValueError):
        parse_parts_option("", available)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_download_selector.py -q -k parse_parts`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement**

```python
def parse_parts_option(raw: str, available: tuple[int, ...]) -> tuple[int, ...]:
    cleaned = raw.strip().casefold()
    if not cleaned:
        raise ValueError("--parts requires a value such as '1,3-5' or 'all'")
    if cleaned == "all":
        return tuple(sorted(available))
    chosen: set[int] = set()
    for token in cleaned.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, _, end_raw = token.partition("-")
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError as exc:
                raise ValueError(f"Invalid part range: {token!r}") from exc
            if end < start:
                raise ValueError(f"Invalid part range: {token!r}")
            chosen.update(n for n in available if start <= n <= end)
        else:
            try:
                number = int(token)
            except ValueError as exc:
                raise ValueError(f"Invalid part number: {token!r}") from exc
            if number not in available:
                raise ValueError(f"Part {number} does not exist in this thread")
            chosen.add(number)
    if not chosen:
        raise ValueError("None of the requested parts exist in this thread")
    return tuple(sorted(chosen))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_download_selector.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/downloads/selector.py tests/unit/test_download_selector.py
git commit -m "feat(downloads): parse --parts selections against detected part numbers"
```

---

### Task 3: One game artifact per selected part

**Files:**
- Modify: `src/vnmaster/downloads/models.py` (add `part` to `PlannedArtifact`)
- Modify: `src/vnmaster/downloads/selector.py`
- Test: `tests/unit/test_download_selector.py`

**Interfaces:**
- Consumes: `PartDetection` / `DetectedPart` from Task 1.
- Produces:
  - `PlannedArtifact.part: str | None = None` (new last field; display label such as "Part 3").
  - `build_download_plan(game, addons, *, platform_priority, preferred_hosts, allow_host_fallback=True, detection: PartDetection | None = None, selected_parts: tuple[int, ...] | None = None) -> DownloadPlan`. With a multipart detection and selected parts, the plan contains one `kind="game"` artifact per selected part, each with `part` set. Parts with no usable mirrors become `SkippedArtifact` entries. Raises `ValueError` if detection is multipart and `selected_parts` is falsy; raises `NoCompatibleDownloadError` if no selected part is downloadable.
  - Embedded add-ons from part-owned groups are excluded; optional groups naming exactly one chosen-family number get that part's label in `artifact.part`.

- [ ] **Step 1: Write the failing tests**

```python
from vnmaster.downloads.selector import detect_parts


def _part_thread() -> ThreadInfo:
    return _thread(9, "Split Game", "v2.0", (
        _group("Part 1 - Win"), _group("Part 1 - Mac"),
        _group("Part 2 - Win"),
        _group("Part 2 walkthrough"),
        _group("Part 3"),          # platform-neutral heading
    ))


def test_multipart_plan_has_one_game_artifact_per_selected_part() -> None:
    game = _part_thread()
    detection = detect_parts(game.downloads)
    plan = build_download_plan(
        game, [], platform_priority=["mac", "windows"], preferred_hosts=["mega"],
        detection=detection, selected_parts=(1, 2),
    )
    games = [a for a in plan.artifacts if a.kind == "game"]
    assert [a.part for a in games] == ["Part 1", "Part 2"]
    assert games[0].platform == "mac"        # per-part platform priority
    assert games[1].platform == "windows"    # part 2 only has a Win group


def test_platform_neutral_part_heading_is_still_downloadable() -> None:
    game = _part_thread()
    detection = detect_parts(game.downloads)
    plan = build_download_plan(
        game, [], platform_priority=["mac", "windows"], preferred_hosts=["mega"],
        detection=detection, selected_parts=(3,),
    )
    games = [a for a in plan.artifacts if a.kind == "game"]
    assert games[0].part == "Part 3"
    assert games[0].platform is None


def test_part_tagged_walkthrough_is_an_addon_bound_to_its_part() -> None:
    game = _part_thread()
    detection = detect_parts(game.downloads)
    plan = build_download_plan(
        game, [], platform_priority=["mac", "windows"], preferred_hosts=["mega"],
        detection=detection, selected_parts=(1, 2),
    )
    addons = [a for a in plan.artifacts if a.kind == "addon"]
    assert any("walkthrough" in a.group_name.casefold() and a.part == "Part 2"
               for a in addons)


def test_multipart_detection_without_selection_raises() -> None:
    game = _part_thread()
    detection = detect_parts(game.downloads)
    with pytest.raises(ValueError):
        build_download_plan(
            game, [], platform_priority=["mac"], preferred_hosts=["mega"],
            detection=detection, selected_parts=None,
        )


def test_single_part_threads_are_unchanged() -> None:
    game = _thread(1, "A Game", "v1.2", (_group("Win/Linux"), _group("Mac")))
    baseline = build_download_plan(
        game, [], platform_priority=["mac", "windows", "linux"],
        preferred_hosts=["mega"],
    )
    with_detection = build_download_plan(
        game, [], platform_priority=["mac", "windows", "linux"],
        preferred_hosts=["mega"], detection=detect_parts(game.downloads),
    )
    assert baseline == with_detection
    assert baseline.artifacts[0].part is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_download_selector.py -q -k "multipart or neutral or tagged or unchanged"`
Expected: FAIL (`part` field / kwargs unknown)

- [ ] **Step 3: Implement**

In `models.py`, add to `PlannedArtifact` after `alternate_mirrors`:

```python
    part: str | None = None
```

In `selector.py`:

1. Extract the platform loop of `_select_game_artifact` (lines 94-115) into a helper so the per-part path reuses it:

```python
def _platform_candidates(
    groups: tuple[tuple[int, DownloadGroup], ...],
    platform_priority: list[str],
    preferred_hosts: list[str],
    allow_host_fallback: bool,
) -> list[DownloadMirror]:
    candidates: list[DownloadMirror] = []
    seen_groups: set[int] = set()
    for platform in platform_priority:
        for group_index, group in groups:
            if group_index in seen_groups:
                continue
            if not _group_matches_platform(group.name, platform):
                continue
            if _REJECT_GAME_GROUP_RE.search(group.name):
                continue
            mirrors = _ordered_mirrors(group, preferred_hosts, allow_host_fallback)
            if mirrors:
                seen_groups.add(group_index)
                candidates.extend(
                    DownloadMirror(
                        mirror.name, mirror.locator,
                        platform=platform, group_name=group.name,
                    )
                    for mirror in mirrors
                )
    return candidates
```

`_select_game_artifact` becomes a thin wrapper calling `_platform_candidates(tuple(enumerate(game.downloads)), ...)` and keeping its current artifact construction and `NoCompatibleDownloadError`.

2. Add the multi-part selection:

```python
def _select_part_artifacts(
    game: ThreadInfo,
    detection: PartDetection,
    selected_parts: tuple[int, ...],
    platform_priority: list[str],
    preferred_hosts: list[str],
    allow_host_fallback: bool,
) -> tuple[list[PlannedArtifact], list[SkippedArtifact]]:
    artifacts: list[PlannedArtifact] = []
    skipped: list[SkippedArtifact] = []
    by_number = {part.number: part for part in detection.parts}
    for number in selected_parts:
        part = by_number.get(number)
        if part is None:
            raise ValueError(f"Part {number} was not detected in this thread")
        part_groups = tuple(
            (index, game.downloads[index]) for index in part.group_indexes
        )
        candidates = _platform_candidates(
            part_groups, platform_priority, preferred_hosts, allow_host_fallback
        )
        if not candidates:
            # Bare "Part 1" headings carry no platform token; keep them
            # eligible as platform-neutral, like add-on groups.
            for _index, group in part_groups:
                candidates.extend(
                    _ordered_mirrors(group, preferred_hosts, allow_host_fallback)
                )
        if not candidates:
            skipped.append(
                SkippedArtifact(
                    f"{game.title} {part.label}", "no downloadable mirrors found"
                )
            )
            continue
        mirror, *alternates = candidates
        artifacts.append(
            PlannedArtifact(
                kind="game",
                title=game.title,
                version=game.version,
                thread_id=game.thread_id,
                thread_url=game.url,
                group_name=mirror.group_name or part.label,
                platform=mirror.platform,
                host=mirror.name,
                locator=mirror.locator,
                alternate_mirrors=tuple(alternates),
                part=part.label,
            )
        )
    if not artifacts:
        raise NoCompatibleDownloadError(
            f"No selected part of {game.title!r} was downloadable"
        )
    return artifacts, skipped
```

3. Rework `build_download_plan`:

```python
def build_download_plan(
    game: ThreadInfo,
    addons: list[ThreadInfo],
    *,
    platform_priority: list[str],
    preferred_hosts: list[str],
    allow_host_fallback: bool = True,
    detection: PartDetection | None = None,
    selected_parts: tuple[int, ...] | None = None,
) -> DownloadPlan:
    skipped: list[SkippedArtifact] = []
    if detection is not None and detection.is_multipart:
        if not selected_parts:
            raise ValueError("Multi-part threads require an explicit part selection")
        game_artifacts, part_skips = _select_part_artifacts(
            game, detection, selected_parts,
            platform_priority, preferred_hosts, allow_host_fallback,
        )
        skipped.extend(part_skips)
    else:
        game_artifacts = [
            _select_game_artifact(
                game, platform_priority, preferred_hosts, allow_host_fallback
            )
        ]
    selected: list[PlannedArtifact] = [
        *game_artifacts,
        *_select_embedded_addons(
            game, game_artifacts, preferred_hosts, detection=detection
        ),
    ]
    # ... existing addon-thread loop unchanged, appending to selected/skipped ...
    return DownloadPlan(game=game, artifacts=tuple(selected), skipped=tuple(skipped))
```

4. `_select_embedded_addons` changes: signature `(game, game_artifacts: list[PlannedArtifact], preferred_hosts, *, detection: PartDetection | None = None)`. Skip any group whose name matches a used game group (`group.name in {a.group_name for a in game_artifacts}`) or, when detection is multipart, any group owned by a detected part (`index in part-owned indexes`). When an optional group's name contains exactly one number of `detection.family` (reuse `_FAMILY_RES[detection.family]`, same single-number rule as `detect_parts`), set `part=f"{detection.family.capitalize()} {number}"` on the produced artifact.

- [ ] **Step 4: Run the full selector suite**

Run: `python -m pytest tests/unit/test_download_selector.py tests/unit/test_download_workflow.py -q`
Expected: PASS (workflow tests confirm no regression through `prepare_download_plan`)

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/downloads/models.py src/vnmaster/downloads/selector.py tests/unit/test_download_selector.py
git commit -m "feat(downloads): plan one game artifact per selected part"
```

---

### Task 4: Two-phase workflow and kind-based optional split

**Files:**
- Modify: `src/vnmaster/downloads/workflow.py`
- Test: `tests/unit/test_download_workflow.py`

**Interfaces:**
- Produces:
  - `ThreadDiscovery(game: ThreadInfo, addons: tuple[ThreadInfo, ...], skipped: tuple[SkippedArtifact, ...])` (frozen dataclass in `workflow.py`)
  - `discover_thread(value: str, *, client, include_addons: bool = True) -> ThreadDiscovery` (the resolve+fetch half of today's `prepare_download_plan`; raises `AmbiguousGameError` exactly as before)
  - `build_plan_from_discovery(discovery, *, platform_priority, preferred_hosts, allow_host_fallback=True, detection=None, selected_parts=None) -> DownloadPlan` (the build half; passes detection kwargs through to `build_download_plan` and appends `discovery.skipped`)
  - `prepare_download_plan(...)` keeps its exact current signature and behavior, now implemented as `build_plan_from_discovery(discover_thread(...))`.
  - `select_optional_artifacts` partitions by kind: every `kind=="game"` artifact is required; numbering applies to add-ons only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_download_workflow.py` (follow that file's existing fixtures for plans; build artifacts with the module's helpers or directly):

```python
from vnmaster.downloads.models import DownloadPlan, PlannedArtifact, ThreadInfo
from vnmaster.downloads.workflow import select_optional_artifacts


def _artifact(kind: str, title: str, part: str | None = None) -> PlannedArtifact:
    return PlannedArtifact(
        kind=kind, title=title, version="v1", thread_id=1,
        thread_url="https://f95zone.to/threads/.1/", group_name=title,
        platform=None, host="MEGA", locator="https://x", part=part,
    )


def _plan(*artifacts: PlannedArtifact) -> DownloadPlan:
    game = ThreadInfo(1, "G", "v1", None, "https://f95zone.to/threads/.1/", ())
    return DownloadPlan(game=game, artifacts=artifacts)


def test_all_game_artifacts_are_required() -> None:
    plan = _plan(
        _artifact("game", "G", part="Part 1"),
        _artifact("game", "G", part="Part 2"),
        _artifact("addon", "G walkthrough"),
    )
    result = select_optional_artifacts(plan, ())
    assert [a.part for a in result.artifacts if a.kind == "game"] == [
        "Part 1", "Part 2",
    ]
    assert all(a.kind == "game" for a in result.artifacts)


def test_optional_numbering_counts_addons_only() -> None:
    plan = _plan(
        _artifact("game", "G", part="Part 1"),
        _artifact("game", "G", part="Part 2"),
        _artifact("addon", "walkthrough"),
        _artifact("addon", "gallery unlocker"),
    )
    result = select_optional_artifacts(plan, (2,))
    addons = [a for a in result.artifacts if a.kind == "addon"]
    assert [a.title for a in addons] == ["gallery unlocker"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_download_workflow.py -q -k "required or numbering"`
Expected: FAIL (today parts 2+ are treated as optional)

- [ ] **Step 3: Implement**

Replace `select_optional_artifacts`:

```python
def select_optional_artifacts(
    candidate_plan: DownloadPlan, selected_numbers: tuple[int, ...]
) -> DownloadPlan:
    """Return required game artifacts plus the selected optional add-ons."""
    required = tuple(a for a in candidate_plan.artifacts if a.kind == "game")
    optional = [a for a in candidate_plan.artifacts if a.kind == "addon"]
    invalid = [n for n in selected_numbers if not 1 <= n <= len(optional)]
    if invalid:
        raise ValueError(f"Optional download number out of range: {invalid[0]}")
    selected = tuple(optional[n - 1] for n in selected_numbers)
    return replace(candidate_plan, artifacts=(*required, *selected))
```

Split `prepare_download_plan`:

```python
@dataclass(frozen=True)
class ThreadDiscovery:
    game: ThreadInfo
    addons: tuple[ThreadInfo, ...]
    skipped: tuple[SkippedArtifact, ...]


def discover_thread(
    value: str, *, client: httpx.Client, include_addons: bool = True
) -> ThreadDiscovery:
    # body = current prepare_download_plan lines 28-54, returning
    # ThreadDiscovery(game, tuple(addons), tuple(discovery_skips))
    ...


def build_plan_from_discovery(
    discovery: ThreadDiscovery,
    *,
    platform_priority: list[str],
    preferred_hosts: list[str],
    allow_host_fallback: bool = True,
    detection: PartDetection | None = None,
    selected_parts: tuple[int, ...] | None = None,
) -> DownloadPlan:
    plan = build_download_plan(
        discovery.game,
        list(discovery.addons),
        platform_priority=platform_priority,
        preferred_hosts=preferred_hosts,
        allow_host_fallback=allow_host_fallback,
        detection=detection,
        selected_parts=selected_parts,
    )
    if discovery.skipped:
        plan = replace(plan, skipped=plan.skipped + discovery.skipped)
    return plan


def prepare_download_plan(value, *, client, platform_priority, preferred_hosts,
                          include_addons=True, allow_host_fallback=True):
    discovery = discover_thread(value, client=client, include_addons=include_addons)
    return build_plan_from_discovery(
        discovery,
        platform_priority=platform_priority,
        preferred_hosts=preferred_hosts,
        allow_host_fallback=allow_host_fallback,
    )
```

Add `from dataclasses import dataclass` and the `PartDetection` import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_download_workflow.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/downloads/workflow.py tests/unit/test_download_workflow.py
git commit -m "feat(downloads): split discovery from plan building, require every game artifact"
```

---

### Task 5: Service refactor, no behavior change

**Files:**
- Modify: `src/vnmaster/downloads/service.py`
- Test: `tests/unit/test_download_service.py` (existing suite is the safety net)

**Interfaces:**
- Produces (internal): `_execute_pairs(pairs: list[tuple[PlannedArtifact, tuple[ResolvedDownload, ...]]], *, final_dir: Path, staging_parent: Path, urm_mods_dir: Path | None, downloader, unpacker, reporter, replace_existing: bool = False) -> DownloadExecutionResult`. This is today's `execute_download_plan_detailed` body (lines 99-237) with two changes: `final_dir` and `staging_parent` are parameters, and when `replace_existing` is true an existing `final_dir` is swapped out (old dir moved aside, new dir renamed in, old dir removed) instead of raising `DestinationExistsError`.
- `execute_download_plan_detailed` keeps its exact signature and semantics, now: normalize candidates, compute `final_dir = destination_root / title / version`, call `_execute_pairs(list(zip(plan.artifacts, candidates)), final_dir=final_dir, staging_parent=destination_root, replace_existing=False, ...)`.

- [ ] **Step 1: Confirm the current suite is green**

Run: `python -m pytest tests/unit/test_download_service.py -q`
Expected: PASS (baseline)

- [ ] **Step 2: Refactor**

Move the body into `_execute_pairs`. The publish block becomes:

```python
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        if final_dir.exists():
            if not replace_existing:
                raise DestinationExistsError(
                    f"Destination already exists; refusing to overwrite it: {final_dir}"
                )
            previous = final_dir.parent / f".vnmaster-previous-{staging.name}"
            final_dir.replace(previous)
            staging.replace(final_dir)
            shutil.rmtree(previous)
        else:
            staging.replace(final_dir)
        published = True
```

The pre-flight `if final_dir.exists(): raise DestinationExistsError` check at the top stays, gated on `not replace_existing`, so a doomed run still fails before downloading gigabytes.

- [ ] **Step 3: Run the suite to prove no behavior change**

Run: `python -m pytest tests/unit/test_download_service.py tests/unit/test_download_state.py -q`
Expected: PASS

- [ ] **Step 4: Add one regression test for the replace path**

```python
def test_execute_pairs_replaces_existing_dir_when_allowed(tmp_path) -> None:
    # Use the file's existing fake downloader/unpacker fixtures to run a
    # single-artifact plan twice into the same final_dir with
    # replace_existing=True via execute_multipart-style call; assert the
    # second run's content wins and no ".vnmaster-previous-" dirs remain.
    ...
```

Write this using the same fake `downloader`/`unpacker` helpers the file already defines (they write marker files); call `_execute_pairs` directly with a one-pair list, run it twice with `replace_existing=True`, assert the marker from run 2 is present and `list(tmp_path.glob(".vnmaster-previous-*")) == []`.

- [ ] **Step 5: Run and commit**

Run: `python -m pytest tests/unit/test_download_service.py -q`
Expected: PASS

```bash
git add src/vnmaster/downloads/service.py tests/unit/test_download_service.py
git commit -m "refactor(downloads): extract pair executor with optional in-place replace"
```

---

### Task 6: Per-part execution

**Files:**
- Modify: `src/vnmaster/downloads/service.py`
- Test: `tests/unit/test_download_service.py`

**Interfaces:**
- Produces:
  - `PartFailure(part: str, error: str)` (frozen dataclass)
  - `MultiPartExecutionResult(version_root: Path, completed: tuple[DownloadExecutionResult, ...], failures: tuple[PartFailure, ...])` (frozen dataclass)
  - `execute_multipart_plan(plan, *, resolved_downloads=None, resolved_urls=None, destination_root: Path, urm_mods_dir: Path | None = None, downloader=download_url, unpacker=unpack_payload, reporter=..., on_part_complete: Callable[[str, DownloadExecutionResult], None] | None = None) -> MultiPartExecutionResult`
  - Each completed part's `DownloadExecutionResult.final_dir` is `version_root / _safe_component(part_label)`.
  - Behavior: version root may pre-exist; parts run sequentially in plan order; part-tagged add-ons ride in their part's transaction; untagged add-ons ride in every part's transaction; a failing part is recorded in `failures` and later parts still run; `on_part_complete` fires after each successful publish.

- [ ] **Step 1: Write the failing tests**

Using the file's existing fake downloader/unpacker pattern (fakes that write predictable files):

```python
from vnmaster.downloads.service import execute_multipart_plan


def _part_plan() -> DownloadPlan:
    # two game artifacts with part labels + one untagged addon,
    # built with the same helpers the file already uses for plans
    ...


def test_multipart_publishes_each_part_into_its_own_dir(tmp_path) -> None:
    result = execute_multipart_plan(
        _part_plan(), resolved_urls=[...], destination_root=tmp_path,
        downloader=fake_downloader, unpacker=fake_unpacker,
    )
    assert result.failures == ()
    assert [r.final_dir.name for r in result.completed] == ["Part 1", "Part 2"]
    assert (result.version_root / "Part 1" / "game").is_dir()
    assert (result.version_root / "Part 2" / "game").is_dir()


def test_multipart_second_part_failure_keeps_the_first(tmp_path) -> None:
    calls = iter([fake_ok, fake_fail_all_mirrors])
    result = execute_multipart_plan(...)
    assert len(result.completed) == 1
    assert result.completed[0].final_dir.name == "Part 1"
    assert result.failures[0].part == "Part 2"
    assert (result.version_root / "Part 1" / "game").is_dir()


def test_multipart_on_part_complete_fires_per_part(tmp_path) -> None:
    seen: list[str] = []
    execute_multipart_plan(
        _part_plan(), ..., on_part_complete=lambda part, _res: seen.append(part),
    )
    assert seen == ["Part 1", "Part 2"]


def test_multipart_refetch_replaces_only_the_chosen_part(tmp_path) -> None:
    # run all parts, then run a plan holding only Part 2 with new content;
    # assert Part 1's files are untouched and Part 2's content changed
    ...
```

Fill the `...` bodies with the file's established fake-plan builders; each game artifact needs `part="Part 1"` / `part="Part 2"` and one resolved URL per artifact.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_download_service.py -q -k multipart`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class PartFailure:
    part: str
    error: str


@dataclass(frozen=True)
class MultiPartExecutionResult:
    version_root: Path
    completed: tuple[DownloadExecutionResult, ...]
    failures: tuple[PartFailure, ...]


def execute_multipart_plan(
    plan: DownloadPlan,
    *,
    resolved_downloads: list[tuple[ResolvedDownload, ...]] | None = None,
    resolved_urls: list[str] | None = None,
    destination_root: Path,
    urm_mods_dir: Path | None = None,
    downloader: Callable[[str, Path], list[Path]] = download_url,
    unpacker: Callable[[list[Path], Path], None] = unpack_payload,
    reporter: Callable[[str], None] = lambda _message: None,
    on_part_complete: Callable[[str, DownloadExecutionResult], None] | None = None,
) -> MultiPartExecutionResult:
    candidates = _normalize_resolved_downloads(
        plan, resolved_downloads=resolved_downloads, resolved_urls=resolved_urls
    )
    pairs = list(zip(plan.artifacts, candidates, strict=True))
    games = [(a, c) for a, c in pairs if a.kind == "game"]
    if any(artifact.part is None for artifact, _ in games):
        raise ValueError("execute_multipart_plan requires part labels on every game")
    tagged = [(a, c) for a, c in pairs if a.kind == "addon" and a.part is not None]
    shared = [(a, c) for a, c in pairs if a.kind == "addon" and a.part is None]

    version = _safe_component(plan.game.version or "unknown-version")
    version_root = destination_root / _safe_component(plan.game.title) / version
    version_root.mkdir(parents=True, exist_ok=True)

    completed: list[DownloadExecutionResult] = []
    failures: list[PartFailure] = []
    for artifact, artifact_candidates in games:
        part_pairs = [
            (artifact, artifact_candidates),
            *[(a, c) for a, c in tagged if a.part == artifact.part],
            *shared,
        ]
        part_dir = version_root / _safe_component(artifact.part)
        reporter(f"Fetching {artifact.part} into {part_dir}...")
        try:
            result = _execute_pairs(
                part_pairs,
                final_dir=part_dir,
                staging_parent=destination_root,
                urm_mods_dir=urm_mods_dir,
                downloader=downloader,
                unpacker=unpacker,
                reporter=reporter,
                replace_existing=True,
            )
        except (ArtifactDownloadError, RuntimeError, OSError) as exc:
            detail = _concise_error(exc)
            failures.append(PartFailure(artifact.part, detail))
            reporter(f"{artifact.part} failed: {detail}")
            continue
        completed.append(result)
        if on_part_complete is not None:
            on_part_complete(artifact.part, result)
    return MultiPartExecutionResult(
        version_root=version_root,
        completed=tuple(completed),
        failures=tuple(failures),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_download_service.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/downloads/service.py tests/unit/test_download_service.py
git commit -m "feat(downloads): execute multi-part plans with per-part transactions"
```

---

### Task 7: Install state merge

**Files:**
- Modify: `src/vnmaster/downloads/state.py`
- Test: `tests/unit/test_download_state.py`

**Interfaces:**
- Produces: `save_install_state(engine, result, *, part: str | None = None, install_root: Path | None = None, reporter=...) -> InstallState`.
  - Legacy call (no `part`): byte-identical behavior to today.
  - Part call: `install_root` required (the version root; `result.final_dir` is `install_root / <part dirname>`). The row is keyed by `str(install_root)`. All relative paths in artifact entries and hash keys gain the `"<part dirname>/"` prefix. Artifact entries gain `"part": <label>`. Merge: keep other parts' artifact entries, hash keys, and verification lines; replace this part's. Verification lines are stored prefixed `"<label>: <check>"`. Row `version`/`platform`/`host` reflect this save; `renpy_game_dir`/`urm_path` are set on row creation and preserved on later part saves.

- [ ] **Step 1: Write the failing tests**

Follow `tests/unit/test_download_state.py`'s existing fixtures (engine fixture, fake `DownloadExecutionResult` builder). Add:

```python
def test_part_save_prefixes_paths_and_records_part(engine, tmp_path) -> None:
    root = tmp_path / "Split Game" / "v2.0"
    result = _fake_result(final_dir=root / "Part 1", part="Part 1")
    state = save_install_state(engine, result, part="Part 1", install_root=root)
    assert state.install_path == root
    entry = state.artifacts[0]
    assert entry["part"] == "Part 1"
    assert entry["archive_paths"][0].startswith("Part 1/")
    assert all(key.startswith("Part 1/") for key in state.archive_hashes)
    assert all(line.startswith("Part 1: ") for line in state.verification)


def test_part_save_merges_and_keeps_sibling_parts(engine, tmp_path) -> None:
    root = tmp_path / "Split Game" / "v2.0"
    save_install_state(engine, _fake_result(final_dir=root / "Part 1", part="Part 1"),
                       part="Part 1", install_root=root)
    save_install_state(engine, _fake_result(final_dir=root / "Part 2", part="Part 2"),
                       part="Part 2", install_root=root)
    state = save_install_state(
        engine, _fake_result(final_dir=root / "Part 1", part="Part 1"),
        part="Part 1", install_root=root,
    )
    parts = sorted({e.get("part") for e in state.artifacts})
    assert parts == ["Part 1", "Part 2"]
    assert any(k.startswith("Part 2/") for k in state.archive_hashes)


def test_legacy_save_is_unchanged(engine, tmp_path) -> None:
    result = _fake_result(final_dir=tmp_path / "G" / "v1")
    state = save_install_state(engine, result)
    assert "part" not in state.artifacts[0] or state.artifacts[0]["part"] is None
    assert not any("/" == k[0] for k in state.archive_hashes)
```

`_fake_result(final_dir, part=None)` mirrors the file's existing result builder; when `part` is given, set it on the game `PlannedArtifact` and create `final_dir` with a small payload file so hashing works.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_download_state.py -q -k part`
Expected: FAIL (unexpected keyword `part`)

- [ ] **Step 3: Implement**

Rework `save_install_state`:

```python
def save_install_state(
    engine: Engine,
    result: DownloadExecutionResult,
    *,
    part: str | None = None,
    install_root: Path | None = None,
    reporter: Callable[[str], None] = lambda _message: None,
) -> InstallState:
    if not result.artifacts:
        raise InstallStateError("Download result has no artifacts")
    if part is not None and install_root is None:
        raise InstallStateError("Part saves require the version root path")
    game_execution = result.artifacts[0]
    game = game_execution.artifact
    prefix = result.final_dir.name if part is not None else None

    def _rel(path: Path | str) -> str:
        return f"{prefix}/{path}" if prefix else str(path)

    archive_hashes: dict[str, str] = {}
    for execution in result.artifacts:
        for relative in execution.archive_paths:
            reporter(f"Hashing preserved payload: {_rel(relative)}")
            archive_hashes[_rel(relative)] = hash_payload(result.final_dir / relative)

    artifacts = []
    for execution in result.artifacts:
        payload = _artifact_payload(execution)
        payload["part"] = part
        payload["output_path"] = _rel(execution.output_path)
        payload["archive_paths"] = [_rel(p) for p in execution.archive_paths]
        artifacts.append(payload)

    verification = tuple(
        f"{part}: {check}" if part else check
        for check in result.verification_checks
    )
    install_path = str(install_root if install_root is not None else result.final_dir)
    now = int(time.time())
    with session_scope(engine) as session:
        row = session.execute(
            select(GameInstall).where(GameInstall.install_path == install_path)
        ).scalar_one_or_none()
        created = row is None
        if created:
            row = GameInstall(..., installed_at=now, updated_at=now)  # as today
            session.add(row)
        if part is not None and not created:
            kept_artifacts = [
                e for e in json.loads(row.artifacts_json or "[]")
                if e.get("part") != part
            ]
            artifacts = kept_artifacts + artifacts
            kept_hashes = {
                k: v for k, v in json.loads(row.archive_hashes_json or "{}").items()
                if not k.startswith(f"{prefix}/")
            }
            archive_hashes = {**kept_hashes, **archive_hashes}
            kept_checks = [
                line for line in json.loads(row.verification_json or "[]")
                if not line.startswith(f"{part}: ")
            ]
            verification = tuple(kept_checks) + verification
        row.f95_thread_id = game.thread_id
        row.game_title = game.title
        row.version = game.version
        row.thread_url = game.thread_url
        row.platform = game_execution.download.platform
        row.host = game_execution.download.host
        row.source_locator = game_execution.download.locator
        row.artifacts_json = json.dumps(artifacts, ensure_ascii=False)
        row.archive_hashes_json = json.dumps(archive_hashes, ensure_ascii=False)
        row.verification_json = json.dumps(list(verification), ensure_ascii=False)
        if created or part is None:
            row.renpy_game_dir = (
                _rel(result.renpy_game_dir) if result.renpy_game_dir is not None else None
            )
            row.urm_path = _rel(result.urm_path) if result.urm_path is not None else None
        row.updated_at = now
        session.flush()
        state = _to_state(row)
    return state
```

Note the legacy path (`part is None`) reduces to exactly today's behavior because `_rel` is the identity and no merge branch runs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_download_state.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/downloads/state.py tests/unit/test_download_state.py
git commit -m "feat(downloads): merge per-part install state instead of replacing the row"
```

---

### Task 8: Per-part rebuild

**Files:**
- Modify: `src/vnmaster/downloads/rebuild.py`
- Test: `tests/unit/test_download_rebuild.py`

**Interfaces:**
- Consumes: state rows whose artifact entries may carry `"part"` and prefixed paths (Task 7).
- Produces: `rebuild_install(state, *, urm_mods_dir, keep_backup=True, unpacker=..., reporter=...) -> RebuildResult` unchanged signature. `RebuildResult.verification_checks` for multi-part installs contains the per-part checks prefixed `"<label>: "`. Legacy installs rebuild exactly as today. Multi-part installs rebuild part by part in label order (numeric sort on the trailing number); a failure raises `RebuildError` after reporting which parts finished; already-rebuilt parts stay rebuilt.

- [ ] **Step 1: Write the failing tests**

Follow the existing fixtures in `tests/unit/test_download_rebuild.py` (they build an `InstallState` plus on-disk payloads):

```python
def test_multipart_rebuild_rebuilds_each_part(tmp_path) -> None:
    state = _multipart_state(tmp_path, parts=("Part 1", "Part 2"))
    result = rebuild_install(state, urm_mods_dir=tmp_path / "Mods",
                             unpacker=fake_unpacker)
    assert (state.install_path / "Part 1" / "game").is_dir()
    assert (state.install_path / "Part 2" / "game").is_dir()
    assert any(c.startswith("Part 1: ") for c in result.verification_checks)
    assert any(c.startswith("Part 2: ") for c in result.verification_checks)


def test_multipart_rebuild_failure_reports_progress(tmp_path) -> None:
    # corrupt Part 2's preserved payload so its checksum fails
    state = _multipart_state(tmp_path, parts=("Part 1", "Part 2"))
    (state.install_path / "Part 2" / "archive" / "payload.zip").write_bytes(b"junk")
    with pytest.raises(RebuildError):
        rebuild_install(state, urm_mods_dir=tmp_path / "Mods",
                        unpacker=fake_unpacker)


def test_legacy_rebuild_leaves_no_part_dirs(tmp_path) -> None:
    state = _single_game_state(tmp_path)  # the file's existing state builder
    rebuild_install(state, urm_mods_dir=tmp_path / "Mods", unpacker=fake_unpacker)
    assert (state.install_path / "game").is_dir()
    assert not any(p.name.startswith("Part ") for p in state.install_path.iterdir())
```

`_multipart_state` builds a state whose `artifacts` entries have `"part"` labels and `"Part N/archive/..."` paths, with matching dirs on disk (`<root>/Part N/game/`, `<root>/Part N/archive/payload.zip`) and `archive_hashes` computed via `hash_payload`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_download_rebuild.py -q -k multipart`
Expected: FAIL (`RebuildError: Recorded install is missing its game directory` because the multi-part root has no top-level `game/`)

- [ ] **Step 3: Implement**

Refactor the current `rebuild_install` body into `_rebuild_single(state) -> RebuildResult` (identical logic). New dispatcher:

```python
def rebuild_install(state, *, urm_mods_dir, keep_backup=True,
                    unpacker=unpack_payload, reporter=lambda _m: None) -> RebuildResult:
    labels = sorted(
        {a.get("part") for a in state.artifacts if a.get("part")},
        key=_part_sort_key,
    )
    if not labels:
        return _rebuild_single(state, urm_mods_dir=urm_mods_dir,
                               keep_backup=keep_backup, unpacker=unpacker,
                               reporter=reporter)
    checks: list[str] = []
    rebuilt: list[str] = []
    for label in labels:
        scoped = _scoped_state(state, label)
        reporter(f"Rebuilding {label}...")
        try:
            result = _rebuild_single(scoped, urm_mods_dir=urm_mods_dir,
                                     keep_backup=keep_backup, unpacker=unpacker,
                                     reporter=reporter)
        except RebuildError as exc:
            done = ", ".join(rebuilt) or "none"
            raise RebuildError(
                f"{label} failed ({exc}); rebuilt so far: {done}"
            ) from exc
        rebuilt.append(label)
        checks.extend(f"{label}: {check}" for check in result.verification_checks)
    return RebuildResult(state.install_path, None, tuple(checks))


def _part_sort_key(label: str) -> tuple[str, int]:
    match = re.search(r"(\d+)\s*$", label)
    return (label if match is None else label[: match.start()],
            int(match.group(1)) if match else 0)


def _scoped_state(state: InstallState, label: str) -> InstallState:
    prefix = label + "/"
    artifacts = []
    for entry in state.artifacts:
        if entry.get("part") != label:
            continue
        scoped = dict(entry)
        scoped["output_path"] = str(entry["output_path"])[len(prefix):]
        scoped["archive_paths"] = [
            str(p)[len(prefix):] for p in entry.get("archive_paths", [])
        ]
        artifacts.append(scoped)
    hashes = {
        key[len(prefix):]: value
        for key, value in state.archive_hashes.items()
        if key.startswith(prefix)
    }
    platform = next(
        (a.get("platform") for a in artifacts if a.get("kind") == "game"), None
    )
    return replace(
        state,
        install_path=state.install_path / label,
        artifacts=tuple(artifacts),
        archive_hashes=hashes,
        platform=platform,
    )
```

(The part dirname equals the label after `_safe_component`; labels like "Part 3" are already filesystem safe. Add `import re` and `from dataclasses import replace`; check `InstallState` is a dataclass, adjust `_scoped_state` to build a new instance if it is not.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_download_rebuild.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/downloads/rebuild.py tests/unit/test_download_rebuild.py
git commit -m "feat(downloads): rebuild multi-part installs part by part"
```

---

### Task 9: CLI wiring

**Files:**
- Modify: `src/vnmaster/cli.py` (fetch command, new helpers)
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `detect_parts`, `parse_parts_option` (selector), `discover_thread` / `build_plan_from_discovery` (workflow), `execute_multipart_plan` (service), `save_install_state(part=..., install_root=...)` (state), `list_install_states`.
- Produces:
  - `--parts TEXT` option on `fetch` ("1,3-5" or "all").
  - `_resolve_part_selection(detection: PartDetection, parts_option: str | None, *, assume_yes: bool, installed: dict[int, str]) -> tuple[int, ...]` (pure, testable): parses `--parts`, errors under `--yes` without `--parts`, otherwise prompts.
  - `_installed_part_versions(engine, thread_id: int) -> dict[int, str]`: part number -> installed version string, read from recorded install rows.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cli.py` (unit-test the helpers directly, no CliRunner needed):

```python
import click
import pytest

from vnmaster.cli import _resolve_part_selection
from vnmaster.downloads.models import DetectedPart, PartDetection


def _detection() -> PartDetection:
    return PartDetection(
        family="part",
        parts=(
            DetectedPart(1, "Part 1", (0,)),
            DetectedPart(2, "Part 2", (1,)),
        ),
    )


def test_parts_flag_is_parsed() -> None:
    assert _resolve_part_selection(
        _detection(), "all", assume_yes=True, installed={}
    ) == (1, 2)


def test_yes_without_parts_is_an_error() -> None:
    with pytest.raises(click.UsageError):
        _resolve_part_selection(_detection(), None, assume_yes=True, installed={})


def test_bad_parts_value_is_a_usage_error() -> None:
    with pytest.raises(click.UsageError):
        _resolve_part_selection(_detection(), "9", assume_yes=True, installed={})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_cli.py -q -k parts`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement the helpers**

In `cli.py` (near the other `_prompt_*` helpers):

```python
def _resolve_part_selection(
    detection: "PartDetection",
    parts_option: str | None,
    *,
    assume_yes: bool,
    installed: dict[int, str],
) -> tuple[int, ...]:
    from vnmaster.downloads.selector import parse_parts_option

    available = tuple(part.number for part in detection.parts)
    if parts_option is not None:
        try:
            return parse_parts_option(parts_option, available)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
    if assume_yes:
        raise click.UsageError(
            "This thread has multiple parts; pass --parts (e.g. --parts 1,3-5 "
            "or --parts all) when using --yes."
        )
    return _prompt_part_selection(detection, installed)


def _prompt_part_selection(
    detection: "PartDetection", installed: dict[int, str]
) -> tuple[int, ...]:
    choices = [
        questionary.Choice(
            title=(
                f"{part.label}"
                + (f" (installed v{installed[part.number]})"
                   if part.number in installed else "")
            ),
            value=part.number,
            checked=False,
        )
        for part in detection.parts
    ]
    answer = questionary.checkbox(
        "This thread is split into multiple games. Pick parts to download:",
        choices=choices,
    ).ask()
    if not answer:
        raise click.ClickException("No parts selected; nothing to do.")
    return tuple(sorted(answer))


def _installed_part_versions(engine, thread_id: int) -> dict[int, str]:
    import re as _re

    from vnmaster.downloads.state import list_install_states

    installed: dict[int, str] = {}
    for state in list_install_states(engine):
        if state.f95_thread_id != thread_id or not state.version:
            continue
        for entry in state.artifacts:
            label = entry.get("part")
            if not isinstance(label, str):
                continue
            match = _re.search(r"(\d+)\s*$", label)
            if match:
                installed[int(match.group(1))] = state.version
    return installed
```

(Mirror the questionary/fallback split the add-on picker uses: if `questionary.checkbox(...).ask()` returns None or the terminal is non-interactive, fall back to a numbered `click.prompt` loop like `_prompt_optional_selection_fallback`.)

- [ ] **Step 4: Rewire fetch**

In `fetch`:

1. Add the option after `--no-addons`:

```python
@click.option(
    "--parts", "parts_option", type=str, default=None,
    help="For multi-part threads: parts to download, e.g. '1,3-5' or 'all'.",
)
```

2. Replace the two `prepare_download_plan(...)` calls with `discover_thread(...)` (same `AmbiguousGameError` retry shape), then:

```python
            from vnmaster.downloads.selector import detect_parts

            detection = detect_parts(discovery.game.downloads)
            for warning in detection.warnings:
                click.echo(f"Note: {warning}")
            if any(
                group.name == "Thread download links"
                for group in discovery.game.downloads
            ):
                # The HTML fallback scraper collapses the OP into one flat
                # link list, so part headings are already gone.
                click.echo(
                    "Note: this thread's links were hand-scraped; multi-part "
                    "detection is unavailable."
                )
            selected_parts: tuple[int, ...] | None = None
            if detection.is_multipart:
                if dry_run and parts_option is None:
                    click.echo("Detected a multi-part thread:")
                    for part in detection.parts:
                        click.echo(f"  {part.number}. {part.label}")
                    click.echo("Re-run with --parts to plan specific parts.")
                    return
                selected_parts = _resolve_part_selection(
                    detection,
                    parts_option,
                    assume_yes=assume_yes,
                    installed=_installed_part_versions(
                        engine, discovery.game.thread_id
                    ),
                )
            elif parts_option is not None:
                raise click.UsageError(
                    "--parts was given but this thread has no detected parts."
                )
            candidate_plan = build_plan_from_discovery(
                discovery,
                platform_priority=cfg.downloads.platform_priority,
                preferred_hosts=(
                    [preferred_host] if preferred_host
                    else cfg.downloads.preferred_hosts
                ),
                detection=detection if detection.is_multipart else None,
                selected_parts=selected_parts,
            )
```

3. Optional add-ons: replace `optional_artifacts = candidate_plan.artifacts[1:]` with `optional_artifacts = tuple(a for a in candidate_plan.artifacts if a.kind == "addon")` (matches the kind-based `select_optional_artifacts` from Task 4).

4. Execution branch (after the resolve loop, replacing the single `execute_download_plan_detailed` call when parts are in play):

```python
        multipart = any(a.kind == "game" and a.part for a in plan.artifacts)
        if multipart:
            from vnmaster.downloads.service import execute_multipart_plan

            saved_ids: list[int] = []

            def _record(part: str, part_result) -> None:
                state = save_install_state(
                    engine, part_result,
                    part=part, install_root=part_result.final_dir.parent,
                    reporter=click.echo,
                )
                saved_ids.append(state.id)

            result = execute_multipart_plan(
                plan,
                resolved_downloads=resolved_downloads,
                destination_root=destination.expanduser(),
                urm_mods_dir=cfg.paths.games_root / "Mods",
                reporter=click.echo,
                on_part_complete=_record,
            )
            for part_result in result.completed:
                click.echo(f"Ready: {part_result.final_dir}")
            if result.failures:
                failed = ", ".join(
                    f"{f.part} ({f.error})" for f in result.failures
                )
                done = ", ".join(
                    r.final_dir.name for r in result.completed
                ) or "none"
                raise click.ClickException(
                    f"Some parts failed: {failed}. Completed: {done}."
                )
            click.echo(f"Recorded install state: #{saved_ids[-1]}")
            return
```

The existing single-game tail (`execute_download_plan_detailed` + `save_install_state` + `Ready:` echo) stays for the non-part path. Also update `_print_download_candidates` and `_print_selected_artifacts` to append `f" [{artifact.part}]"` to the title line when `artifact.part` is set.

- [ ] **Step 5: Run the CLI suite**

Run: `python -m pytest tests/unit/test_cli.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/vnmaster/cli.py tests/unit/test_cli.py
git commit -m "feat(cli): part selection, --parts flag, and per-part fetch execution"
```

---

### Task 10: Scanner depth

**Files:**
- Modify: `src/vnmaster/scanners/disk.py:28`
- Test: `tests/unit/test_scanners_disk.py`

**Interfaces:**
- Produces: `scan_disk(root, max_depth=3)`. Parts sit at `<Game>/<version>/<part>/`, one level below today's layout, so the bounded walk needs one more level. Early stop at recognized games is unchanged.

- [ ] **Step 1: Write the failing test**

```python
def test_finds_games_in_part_subdirs(tmp_path) -> None:
    build = tmp_path / "Split Game" / "v2.0" / "Part 2" / "game" / "SplitGame-pc"
    # scan_disk recognizes a dir with renpy/, game/, and a launcher
    (build / "renpy").mkdir(parents=True)
    (build / "game").mkdir()
    (build / "SplitGame.sh").touch()
    names = {g.folder_name for g in scan_disk(tmp_path, max_depth=5)}
    assert "SplitGame-pc" in names
```

Check where recognized game dirs actually sit for vnmaster installs (the build dir under `<part>/game/`); pick `max_depth` in the default so that this test passes WITHOUT passing `max_depth` explicitly, then drop the explicit argument from the test. The point of the task is the new default.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_scanners_disk.py -q -k part_subdirs`
Expected: FAIL (game not found at the deeper level)

- [ ] **Step 3: Bump the default**

Change `def scan_disk(root: Path, max_depth: int = 2)` so the new default reaches part layouts (one more level than whatever depth finds current installs; the test from Step 1 is the arbiter). Update the module docstring's "bounded number of levels" sentence to mention part subdirs.

- [ ] **Step 4: Run the scanner suite**

Run: `python -m pytest tests/unit/test_scanners_disk.py -q`
Expected: PASS, including the existing early-stop and fixture tests

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/scanners/disk.py tests/unit/test_scanners_disk.py
git commit -m "fix(scanners): reach games installed in per-part subdirs"
```

---

### Task 11: Matcher aggregation

**Files:**
- Modify: `src/vnmaster/matcher.py` (the `for d in installed:` merge block around line 170)
- Test: `tests/unit/test_matcher.py`

**Interfaces:**
- Produces: when several `InstalledGame` entries resolve to the same thread id, the merged `LibraryMatch` keeps the first entry's `install_path` parent chain (the shared version root if the paths are siblings, else the first path), sums `disk_size_bytes`, and keeps the first non-None `installed_version`. No new fields on `LibraryMatch`.

- [ ] **Step 1: Write the failing test**

Follow `tests/unit/test_matcher.py`'s existing fixture style for `InstalledGame` and F95 rows:

```python
def test_multiple_part_installs_aggregate_to_one_entry() -> None:
    parts = [
        _installed("Part 1", install_path=Path("/g/Split Game/v2.0/Part 1"),
                   version="v2.0", size=100),
        _installed("Part 2", install_path=Path("/g/Split Game/v2.0/Part 2"),
                   version="v2.0", size=250),
    ]
    result = match_library(..., installed=parts, ...)
    matches = [m for m in result.matches if m.f95_thread_id == SPLIT_TID]
    assert len(matches) == 1
    assert matches[0].disk_size_bytes == 350
    assert matches[0].install_path == Path("/g/Split Game/v2.0")
    assert matches[0].installed_version == "v2.0"
```

Adapt `_installed(...)` and the match call to the file's real fixture helpers; both entries must fuzzy-resolve to the same thread (give them folder names containing the game title, e.g. "Split Game Part 1").

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_matcher.py -q -k aggregate`
Expected: FAIL (size is 250, last part wins)

- [ ] **Step 3: Implement**

In the `for d in installed:` loop, replace the overwrite branch:

```python
        existing = by_thread.get(tid)
        if existing is not None and existing.install_path is not None:
            # Several installed dirs for one thread: a multi-part install.
            # Aggregate instead of last-one-wins.
            shared_root = (
                existing.install_path.parent
                if existing.install_path.parent == d.install_path.parent
                else existing.install_path
            )
            by_thread[tid] = LibraryMatch(
                **{
                    **existing.__dict__,
                    "install_path": shared_root,
                    "installed_version": existing.installed_version
                    or d.installed_version,
                    "disk_size_bytes": (existing.disk_size_bytes or 0)
                    + (d.disk_size_bytes or 0),
                }
            )
        elif existing is not None:
            # existing came from play history only; today's overwrite is right
            by_thread[tid] = LibraryMatch(
                **{**existing.__dict__, "install_path": d.install_path,
                   "installed_version": d.installed_version,
                   "disk_size_bytes": d.disk_size_bytes}
            )
        else:
            # unchanged creation branch
            ...
```

- [ ] **Step 4: Run the matcher suite**

Run: `python -m pytest tests/unit/test_matcher.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/matcher.py tests/unit/test_matcher.py
git commit -m "fix(matcher): aggregate multi-part installs into one library entry"
```

---

### Task 12: Full-suite verification and docs

**Files:**
- Modify: `README.md` (fetch section: document `--parts` and multi-part behavior)

- [ ] **Step 1: Run the whole test suite**

Run: `python -m pytest tests -q`
Expected: PASS. Fix anything broken before proceeding; the single-part regression promise (Global Constraints) is the priority.

- [ ] **Step 2: Update README**

In the fetch documentation, add a short subsection:

```markdown
### Multi-part games

Some devs split one story into several discrete games in a single thread
("Part 1".."Part 7"). `vnmaster fetch` detects this from the download group
names and asks which parts to download (nothing is pre-selected). Each part
installs into its own subdir under the version folder and can be added or
re-downloaded later by running fetch again.

Non-interactive runs must be explicit: `--yes` requires `--parts`, e.g.
`vnmaster fetch "grandma's house" --yes --parts 1-3` or `--parts all`.
Threads whose download section only exposes a flat link list (no group
headings) cannot be detected; fetch warns and treats them as a single game.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: describe multi-part game fetching and the --parts flag"
```

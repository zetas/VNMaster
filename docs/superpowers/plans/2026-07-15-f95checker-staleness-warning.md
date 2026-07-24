# F95Checker Staleness Warning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Warn in the Discord digest when F95Checker's library data is more than 24 hours old, including on otherwise-silent nightly runs.

**Architecture:** One new read method on the existing read-only `F95CheckerDB` accessor returns the `settings.last_successful_refresh` epoch (or `None` when unknowable). The pipeline computes a warning string once per run via a pure helper, posts it standalone when a stale daily run would otherwise exit silently, and appends it to the kickoff text whenever a digest posts.

**Tech Stack:** Python 3.12, SQLAlchemy (raw `text()` SQL against F95Checker's sqlite), pytest + pytest-asyncio, unittest.mock. No new dependencies.

Spec: `docs/superpowers/specs/2026-07-15-f95checker-staleness-warning-design.md`

## Global Constraints

- Commit messages: conventional commits, plain language, and no generated attribution.
- Never write to F95Checker's db; the accessor stays read-only.
- `None` from the accessor (missing table, missing column, value 0) means "can't assess": no warning, behavior unchanged.
- Staleness threshold: `STALE_REFRESH_SECONDS = 24 * 3600`, module constant in `pipeline.py`, not configurable.
- Warning copy, exactly: `F95Checker data is stale — last successful refresh was {age} ({stamp}). Open F95Checker and hit Refresh.` where `{age}` is `"{N} hours ago"` below 48h and `"{N} days ago"` at or above, and `{stamp}` is local time `%b %d %H:%M`. The em dash in this product copy matches existing kickoff copy and is intentional.
- `@everyone ` is prefixed to the warning only when the surrounding message doesn't already contain `@everyone`.
- Run tests with: `uv run pytest <path> -v` from the repo root.

---

### Task 1: `last_successful_refresh_epoch()` accessor

**Files:**
- Modify: `src/vnmaster/db/ro_f95checker.py` (add one method to `F95CheckerDB`, class ends around line 111)
- Test: `tests/unit/test_db_ro_f95checker.py` (append)

**Interfaces:**
- Consumes: existing `F95CheckerDB._engine` (SQLAlchemy Engine), `sqlalchemy.inspect`, `sqlalchemy.text` (both already imported in the module).
- Produces: `F95CheckerDB.last_successful_refresh_epoch(self) -> int | None`. Task 3 calls this from the pipeline.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_db_ro_f95checker.py`:

```python
def test_last_successful_refresh_returns_epoch(tmp_path: Path) -> None:
    db_file = tmp_path / "with_settings.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE games (id INTEGER PRIMARY KEY, name TEXT, "
        "version TEXT, last_updated INTEGER)"
    )
    conn.execute("CREATE TABLE settings (last_successful_refresh INTEGER)")
    conn.execute("INSERT INTO settings VALUES (1784097846)")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() == 1784097846


def test_last_successful_refresh_zero_means_none(tmp_path: Path) -> None:
    """0 is F95Checker's never-refreshed default; treat as unknowable."""
    db_file = tmp_path / "zero.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (last_successful_refresh INTEGER)")
    conn.execute("INSERT INTO settings VALUES (0)")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() is None


def test_last_successful_refresh_missing_column(tmp_path: Path) -> None:
    db_file = tmp_path / "no_col.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (bg_refresh_interval INTEGER)")
    conn.execute("INSERT INTO settings VALUES (30)")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() is None


def test_last_successful_refresh_missing_table() -> None:
    """The checked-in fixture has a games table but no settings table."""
    db = F95CheckerDB.open(FIXTURE)
    assert db.last_successful_refresh_epoch() is None


def test_last_successful_refresh_empty_settings(tmp_path: Path) -> None:
    db_file = tmp_path / "empty.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (last_successful_refresh INTEGER)")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_db_ro_f95checker.py -v -k last_successful`
Expected: 5 FAILED with `AttributeError: 'F95CheckerDB' object has no attribute 'last_successful_refresh_epoch'`

- [ ] **Step 3: Implement the accessor**

Add to `F95CheckerDB` in `src/vnmaster/db/ro_f95checker.py`, after `iter_games_updated_since` (docstring explains the None contract, matching the module's schema-tolerance style):

```python
    def last_successful_refresh_epoch(self) -> int | None:
        """Epoch of F95Checker's last completed full-refresh pass.

        Returns None when the settings table or column is absent (other
        F95Checker versions) or the value is 0 (never refreshed). None means
        "can't assess staleness", not "stale".
        """
        insp = inspect(self._engine)
        if "settings" not in insp.get_table_names():
            return None
        cols = {c["name"] for c in insp.get_columns("settings")}
        if "last_successful_refresh" not in cols:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT last_successful_refresh FROM settings")
            ).first()
        if row is None or not row[0]:
            return None
        return int(row[0])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_db_ro_f95checker.py -v`
Expected: all PASS (the 5 new plus the 5 existing)

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/db/ro_f95checker.py tests/unit/test_db_ro_f95checker.py
git commit -m "feat(db): read last_successful_refresh from f95checker settings"
```

---

### Task 2: `_stale_warning` helper

**Files:**
- Modify: `src/vnmaster/pipeline.py` (module constant + helper function; place both near the other module-level helpers after `run_digest_pipeline`)
- Test: Create `tests/unit/test_pipeline_stale.py`

**Interfaces:**
- Consumes: nothing from Task 1 (pure function).
- Produces: `STALE_REFRESH_SECONDS: int = 24 * 3600` and `_stale_warning(last_refresh_epoch: int | None, now_epoch: int) -> str | None`, both module-level in `vnmaster.pipeline`. Task 3 calls the helper with Task 1's accessor value.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_pipeline_stale.py`. Note: `_stale_warning` formats the timestamp in local time, so tests compute the expected stamp with the same `datetime.fromtimestamp` call instead of hardcoding clock strings.

```python
"""Tests for the digest staleness warning helper."""
from __future__ import annotations

from datetime import datetime

from vnmaster.pipeline import STALE_REFRESH_SECONDS, _stale_warning

NOW = 1_784_100_000


def _stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%b %d %H:%M")


def test_fresh_refresh_returns_none() -> None:
    last = NOW - 3600
    assert _stale_warning(last, NOW) is None


def test_exactly_at_threshold_returns_none() -> None:
    last = NOW - STALE_REFRESH_SECONDS
    assert _stale_warning(last, NOW) is None


def test_none_input_returns_none() -> None:
    assert _stale_warning(None, NOW) is None


def test_stale_below_48h_renders_hours() -> None:
    last = NOW - 31 * 3600
    warning = _stale_warning(last, NOW)
    assert warning == (
        "F95Checker data is stale — last successful refresh was "
        f"31 hours ago ({_stamp(last)}). Open F95Checker and hit Refresh."
    )


def test_stale_at_48h_and_above_renders_days() -> None:
    last = NOW - 3 * 86400
    warning = _stale_warning(last, NOW)
    assert warning == (
        "F95Checker data is stale — last successful refresh was "
        f"3 days ago ({_stamp(last)}). Open F95Checker and hit Refresh."
    )


def test_boundary_47h_hours_48h_days() -> None:
    warning_47 = _stale_warning(NOW - 47 * 3600, NOW)
    warning_48 = _stale_warning(NOW - 48 * 3600, NOW)
    assert "47 hours ago" in warning_47
    assert "2 days ago" in warning_48


def test_warning_has_no_ping() -> None:
    """Call sites decide whether to prefix @everyone; the helper never does."""
    warning = _stale_warning(NOW - 3 * 86400, NOW)
    assert "@everyone" not in warning
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_pipeline_stale.py -v`
Expected: FAIL at import with `ImportError: cannot import name 'STALE_REFRESH_SECONDS'`

- [ ] **Step 3: Implement the helper**

In `src/vnmaster/pipeline.py`, add to the imports at the top:

```python
from datetime import datetime
```

Add below the existing `log = get_logger(__name__)` line:

```python
STALE_REFRESH_SECONDS = 24 * 3600
```

Add this function after `run_digest_pipeline`, alongside the other `_`-prefixed helpers:

```python
def _stale_warning(last_refresh_epoch: int | None, now_epoch: int) -> str | None:
    """Warning line when F95Checker hasn't refreshed in over 24h, else None.

    None input means staleness can't be assessed (old schema or never
    refreshed) and suppresses the warning. Age is >= 24h whenever this
    returns text, so the hour/day wording never needs a singular form.
    """
    if last_refresh_epoch is None:
        return None
    age = now_epoch - last_refresh_epoch
    if age <= STALE_REFRESH_SECONDS:
        return None
    if age < 48 * 3600:
        age_text = f"{age // 3600} hours ago"
    else:
        age_text = f"{age // 86400} days ago"
    stamp = datetime.fromtimestamp(last_refresh_epoch).strftime("%b %d %H:%M")
    return (
        f"F95Checker data is stale — last successful refresh was "
        f"{age_text} ({stamp}). Open F95Checker and hit Refresh."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_pipeline_stale.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/pipeline.py tests/unit/test_pipeline_stale.py
git commit -m "feat(pipeline): stale-refresh warning helper"
```

---

### Task 3: Wire the warning into the pipeline

**Files:**
- Modify: `src/vnmaster/pipeline.py` (`run_digest_pipeline`: the schema-check block near line 60, the daily early-return near line 91, and the kickoff construction near lines 130-139)
- Modify: `tests/integration/test_pipeline_e2e.py` (all five existing tests' `f95_db` mocks, plus new tests)

**Interfaces:**
- Consumes: `deps.f95_db.last_successful_refresh_epoch()` (Task 1), `_stale_warning(...)` (Task 2).
- Produces: user-visible behavior only; nothing downstream consumes new symbols.

- [ ] **Step 1: Fix existing e2e mocks so they survive the new accessor call**

`run_digest_pipeline` will now call `deps.f95_db.last_successful_refresh_epoch()`. The existing tests build `f95_db = MagicMock()`, whose auto-created method would return a truthy `MagicMock` and crash the age arithmetic. In `tests/integration/test_pipeline_e2e.py`, every place an `f95_db` mock is configured (six of them: `f95_db` in the first four tests, and both `f95_db` and `f95_db_weekly` in the last test), add one line right after `check_schema` is set:

```python
    f95_db.last_successful_refresh_epoch = MagicMock(return_value=None)
```

(and correspondingly `f95_db_weekly.last_successful_refresh_epoch = MagicMock(return_value=None)`).

- [ ] **Step 2: Write the failing tests**

Append to `tests/integration/test_pipeline_e2e.py`. These reuse the module's existing imports (`SimpleNamespace`, `AsyncMock`, `MagicMock`, `Config`, `LibraryGame`, `ExtractionResult`, etc.).

```python
def _stale_f95_db(rows, *, last_refresh):
    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.iter_all_games = MagicMock(return_value=iter(rows))
    f95_db.last_successful_refresh_epoch = MagicMock(return_value=last_refresh)
    return f95_db


def _eternum_row(version="0.7.0", status="1", last_updated=100):
    return SimpleNamespace(
        id=42, name="Eternum", version=version, developer="Caribdis",
        engine="Ren'Py", status=status, last_updated=last_updated,
        changelog="notes", description="", image_url=None, tags=[],
        executable=None, archived=0, rating=4,
    )


@pytest.mark.asyncio
async def test_daily_stale_and_quiet_posts_standalone_warning(tmp_path, monkeypatch):
    """A stale db on a no-updates night must warn instead of staying silent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.7.0",
            latest_upstream_version="0.7.0", last_daily_notified_version="0.7.0",
            last_daily_notified_status="1", status="1",
            created_at=1, updated_at=1,
        ))

    now = 1_784_100_000
    f95_db = _stale_f95_db([_eternum_row()], last_refresh=now - 3 * 86400)
    webhook = AsyncMock()
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=MagicMock(), webhook=webhook, bot=bot, config=cfg,
        now_epoch=now, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    assert webhook.send.await_count == 1
    content = webhook.send.call_args.kwargs.get("content") or ""
    assert content.startswith("@everyone F95Checker data is stale")
    assert "3 days ago" in content
    with session_scope(engine) as s:
        assert s.execute(select(DigestRun)).scalars().all() == []


@pytest.mark.asyncio
async def test_daily_fresh_and_quiet_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.7.0",
            latest_upstream_version="0.7.0", last_daily_notified_version="0.7.0",
            last_daily_notified_status="1", status="1",
            created_at=1, updated_at=1,
        ))

    now = 1_784_100_000
    f95_db = _stale_f95_db([_eternum_row()], last_refresh=now - 3600)
    webhook = AsyncMock()
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=MagicMock(), webhook=webhook, bot=bot, config=cfg,
        now_epoch=now, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    assert webhook.send.await_count == 0


@pytest.mark.asyncio
async def test_daily_stale_with_updates_appends_warning_without_second_ping(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.5.0",
            latest_upstream_version="0.5.0", last_daily_notified_version="0.5.0",
            last_daily_notified_status="1", status="1",
            created_at=1, updated_at=1,
        ))

    now = 1_784_100_000
    f95_db = _stale_f95_db(
        [_eternum_row(last_updated=300)], last_refresh=now - 3 * 86400
    )
    llm_cache = MagicMock()
    llm_cache.extract_for.return_value = ExtractionResult(
        method="llm",
        versions=[{"version": "0.7.0", "renders": 400, "bugfix_only": False,
                   "summary_one_line": "story"}],
        cost_usd=0.004,
    )
    _msg_ids = iter(["m0", "m1", "m2"])
    webhook = AsyncMock()
    webhook.send.side_effect = lambda *a, **kw: MagicMock(id=next(_msg_ids))
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=llm_cache, webhook=webhook, bot=bot, config=cfg,
        now_epoch=now, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    kickoff = webhook.send.call_args_list[0].kwargs.get("content") or ""
    assert "F95Checker data is stale" in kickoff
    # The daily kickoff already pings; the warning line must not add another.
    assert kickoff.count("@everyone") == 1


@pytest.mark.asyncio
async def test_weekly_stale_appends_warning_with_ping(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    now = 1_784_100_000
    f95_db = _stale_f95_db([], last_refresh=now - 3 * 86400)
    webhook = AsyncMock()
    webhook.send.side_effect = lambda *a, **kw: MagicMock(id="m0")
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=MagicMock(), webhook=webhook, bot=bot, config=cfg,
        now_epoch=now, channel_id="c-1", mode="weekly",
    )
    await run_digest_pipeline(deps)

    kickoff = webhook.send.call_args_list[0].kwargs.get("content") or ""
    assert kickoff.startswith("Weekly digest")
    assert "@everyone F95Checker data is stale" in kickoff
```

- [ ] **Step 3: Run new tests to verify they fail**

Run: `uv run pytest tests/integration/test_pipeline_e2e.py -v -k "stale or fresh_and_quiet"`
Expected: `test_daily_fresh_and_quiet_stays_silent` PASSES (current behavior is silence); the other three FAIL (no warning is posted/appended yet)

- [ ] **Step 4: Wire the pipeline**

In `src/vnmaster/pipeline.py`, `run_digest_pipeline`:

Change step 1 (currently lines 60-62) to also compute the warning:

```python
    # 1. Schema check + read F95Checker
    deps.f95_db.check_schema()
    f95_rows = list(deps.f95_db.iter_all_games())
    stale_warning = _stale_warning(
        deps.f95_db.last_successful_refresh_epoch(), deps.now_epoch
    )
```

Change the daily early-return (currently lines 91-93):

```python
        if not candidates.updates:
            if stale_warning:
                log.warning("daily check: no new updates but F95Checker is stale")
                await deps.webhook.send(content=f"@everyone {stale_warning}")
            else:
                log.info("daily check: no new updates; nothing to post")
            return
```

After the kickoff construction (currently lines 130-139, the `if deps.mode == "daily": ... else: ...` chain), append the warning line before `poster.post`:

```python
    if stale_warning:
        ping = "" if "@everyone" in kickoff else "@everyone "
        kickoff = f"{kickoff}\n{ping}{stale_warning}"
```

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest tests/unit tests/integration -v -x --ignore=tests/integration/test_llm_changelog_live.py`
Expected: all PASS (the live-LLM test is excluded; it needs an API key)

- [ ] **Step 6: Commit**

```bash
git add src/vnmaster/pipeline.py tests/integration/test_pipeline_e2e.py
git commit -m "feat(digest): warn when f95checker data is over 24h stale"
```

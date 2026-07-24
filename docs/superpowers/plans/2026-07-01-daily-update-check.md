# Daily Update Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a nightly `vnmaster digest --daily` run that alerts Discord (with `@everyone`) the first time a tracked game gets a new version or notable status change, stays silent when there's nothing new, and leaves the Saturday weekly digest unchanged.

**Architecture:** The daily run reuses the existing digest pipeline in a new `mode="daily"`. The mode gates four things: it selects candidates by a version/status "already-notified" watermark instead of the weekly throttle, it never writes weekly-owned state (`last_seen_in_digest_at`, `status`/`status_changed`, or the weekly "since" pointer), it prefixes the kickoff with `@everyone`, and it returns without posting when there are no candidates. Two new `library_games` columns hold the per-game daily watermark; a new `digest_runs.kind` column keeps daily runs from moving the weekly pointer.

**Tech Stack:** Python 3.14, Click, SQLAlchemy 2.x (SQLite), Alembic, discord.py, pytest.

## Global Constraints

- Weekly digest behavior must stay byte-for-byte identical. Daily runs must not write `last_seen_in_digest_at`, `status`, `status_changed`, and must not move the weekly "since last digest" pointer.
- `ensure_schema()` (not Alembic) is the live-DB migration path; every schema change must be added to both `_ensure_extra_columns()` and a new Alembic migration.
- Style rules (commits, comments): conventional commits `type(scope): subject`, imperative, lowercase type. No em-dashes, no AI-isms, no attribution/Co-Authored-By lines.
- SQLite ALTER via `op.batch_alter_table` in migrations.
- The daily launchd job runs every day: its plist uses `StartCalendarInterval` with Hour/Minute only (no `Weekday` key).
- Default daily schedule: `"0 1 * * *"` (1am daily).

---

### Task 1: Schema — daily watermark columns + digest_runs.kind

**Files:**
- Modify: `src/vnmaster/db/models.py:42-52` (LibraryGame columns) and `src/vnmaster/db/models.py:77-86` (DigestRun)
- Modify: `src/vnmaster/db/engine.py:41-64` (`_ensure_extra_columns`)
- Create: `src/vnmaster/db/migrations/versions/b2c3d4e5f6a7_daily_check_columns.py`
- Test: `tests/unit/test_db_engine.py` (new test), `tests/integration/test_migrations.py` (extend)

**Interfaces:**
- Produces: `LibraryGame.last_daily_notified_version: str | None`, `LibraryGame.last_daily_notified_status: str | None`, `DigestRun.kind: str` (default `"weekly"`). `_ensure_extra_columns` self-heals all three on existing DBs, backfilling the two library columns from `latest_upstream_version` / `status`.

- [ ] **Step 1: Write the failing test for the self-heal path**

Add to `tests/unit/test_db_engine.py`:

```python
def test_ensure_schema_adds_daily_columns_and_backfills(tmp_path) -> None:
    from sqlalchemy import text
    from vnmaster.db.engine import create_engine_for, ensure_schema

    db = tmp_path / "old.db"
    engine = create_engine_for(db)
    # Simulate an old DB: library_games without the daily columns, one row.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE library_games ("
            "f95_thread_id INTEGER PRIMARY KEY, game_title VARCHAR NOT NULL, "
            "latest_upstream_version VARCHAR, status VARCHAR, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO library_games "
            "(f95_thread_id, game_title, latest_upstream_version, status, created_at, updated_at) "
            "VALUES (7, 'Eternum', '0.7.0', '2', 1, 1)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE digest_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, run_at INTEGER NOT NULL, "
            "updates_count INTEGER NOT NULL, llm_calls INTEGER NOT NULL, "
            "llm_cost_usd FLOAT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO digest_runs (run_at, updates_count, llm_calls, llm_cost_usd) "
            "VALUES (100, 0, 0, 0.0)"
        )

    ensure_schema(engine)

    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT last_daily_notified_version, last_daily_notified_status "
            "FROM library_games WHERE f95_thread_id = 7"
        )).one()
        assert row[0] == "0.7.0"   # backfilled from latest_upstream_version
        assert row[1] == "2"        # backfilled from status
        kind = conn.execute(text("SELECT kind FROM digest_runs WHERE run_at = 100")).scalar_one()
        assert kind == "weekly"     # backfilled default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_db_engine.py::test_ensure_schema_adds_daily_columns_and_backfills -v`
Expected: FAIL (no such column: last_daily_notified_version).

- [ ] **Step 3: Add the model columns**

In `src/vnmaster/db/models.py`, inside `LibraryGame`, after the `last_seen_in_digest_at` line (currently line 49) add:

```python
    # Per-game watermark for the nightly (daily) alert: the upstream version and
    # notable status last alerted via a daily run. Baselined on insert so the
    # existing backlog doesn't fire on the first nightly run.
    last_daily_notified_version: Mapped[str | None] = mapped_column(String, nullable=True)
    last_daily_notified_status: Mapped[str | None] = mapped_column(String, nullable=True)
```

In `DigestRun`, after the `llm_cost_usd` column (currently line 84) add:

```python
    # 'weekly' or 'daily'. Only 'weekly' runs move the "since last digest" pointer.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="weekly")
```

- [ ] **Step 4: Extend `_ensure_extra_columns` to add + backfill the new columns**

Replace the body of `_ensure_extra_columns` in `src/vnmaster/db/engine.py` (currently lines 41-64) with:

```python
def _ensure_extra_columns(engine: Engine) -> None:
    """Add columns introduced after a table was first created.

    `create_all` never ALTERs existing tables, so a model column added later
    won't appear on an already-created table. This idempotently adds such
    columns (defaulted/backfilled) so old DBs self-heal on the next run.
    """
    with engine.begin() as conn:
        lib_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(library_games)")
        }
        if "status_changed" not in lib_cols:
            conn.exec_driver_sql(
                "ALTER TABLE library_games ADD COLUMN status_changed INTEGER NOT NULL DEFAULT 0"
            )
        if "last_daily_notified_version" not in lib_cols:
            conn.exec_driver_sql(
                "ALTER TABLE library_games ADD COLUMN last_daily_notified_version VARCHAR"
            )
            conn.exec_driver_sql(
                "UPDATE library_games SET last_daily_notified_version = latest_upstream_version "
                "WHERE last_daily_notified_version IS NULL"
            )
        if "last_daily_notified_status" not in lib_cols:
            conn.exec_driver_sql(
                "ALTER TABLE library_games ADD COLUMN last_daily_notified_status VARCHAR"
            )
            conn.exec_driver_sql(
                "UPDATE library_games SET last_daily_notified_status = status "
                "WHERE last_daily_notified_status IS NULL"
            )
        run_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(digest_runs)")
        }
        if "kind" not in run_cols:
            conn.exec_driver_sql(
                "ALTER TABLE digest_runs ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'weekly'"
            )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_db_engine.py::test_ensure_schema_adds_daily_columns_and_backfills -v`
Expected: PASS.

- [ ] **Step 6: Write the Alembic migration**

Create `src/vnmaster/db/migrations/versions/b2c3d4e5f6a7_daily_check_columns.py`:

```python
"""add daily-check watermark columns and digest_runs.kind

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-01 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('digest_runs') as batch_op:
        batch_op.add_column(
            sa.Column('kind', sa.String(), nullable=False, server_default='weekly')
        )
    with op.batch_alter_table('library_games') as batch_op:
        batch_op.add_column(
            sa.Column('last_daily_notified_version', sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('last_daily_notified_status', sa.String(), nullable=True)
        )
    op.execute(
        "UPDATE library_games SET last_daily_notified_version = latest_upstream_version"
    )
    op.execute(
        "UPDATE library_games SET last_daily_notified_status = status"
    )


def downgrade() -> None:
    with op.batch_alter_table('library_games') as batch_op:
        batch_op.drop_column('last_daily_notified_status')
        batch_op.drop_column('last_daily_notified_version')
    with op.batch_alter_table('digest_runs') as batch_op:
        batch_op.drop_column('kind')
```

- [ ] **Step 7: Extend the migration integration test**

In `tests/integration/test_migrations.py`, at the end of `test_alembic_upgrade_head_creates_all_tables`, add:

```python
    lib_cols = {c["name"] for c in inspect(engine).get_columns("library_games")}
    assert {"last_daily_notified_version", "last_daily_notified_status"} <= lib_cols
    run_cols = {c["name"] for c in inspect(engine).get_columns("digest_runs")}
    assert "kind" in run_cols
```

- [ ] **Step 8: Run the schema tests**

Run: `uv run pytest tests/unit/test_db_engine.py tests/integration/test_migrations.py tests/unit/test_db_models.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/vnmaster/db/models.py src/vnmaster/db/engine.py src/vnmaster/db/migrations/versions/b2c3d4e5f6a7_daily_check_columns.py tests/unit/test_db_engine.py tests/integration/test_migrations.py
git commit -m "feat(db): add daily-notified watermark columns and digest_runs.kind"
```

---

### Task 2: Config + launchd daily plist

**Files:**
- Modify: `src/vnmaster/config.py:48-50` (ScheduleConfig)
- Modify: `src/vnmaster/launchd.py` (cron parser, daily template, render + install)
- Test: `tests/unit/test_launchd.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces: `ScheduleConfig.daily_cron: str = "0 1 * * *"`; `parse_simple_cron` returns `SimpleCron` whose `weekday` is `int | None` (None when the field is `*`); `render_daily_plist(*, bin_path: Path, log_dir: Path, cron: str) -> str`; `install_daily_plist(*, daily_text: str, launchagents_dir: Path) -> Path`.

- [ ] **Step 1: Write failing tests for the cron parser and daily plist**

Add to `tests/unit/test_launchd.py`:

```python
def test_parse_cron_accepts_wildcard_weekday() -> None:
    from vnmaster.launchd import parse_simple_cron
    parsed = parse_simple_cron("0 1 * * *")
    assert parsed.minute == 0
    assert parsed.hour == 1
    assert parsed.weekday is None


def test_render_daily_plist_has_no_weekday_and_uses_daily_flag(tmp_path) -> None:
    from vnmaster.launchd import render_daily_plist
    out = render_daily_plist(
        bin_path=Path("/opt/bin/vnmaster"),
        log_dir=Path("/Users/x/Logs"),
        cron="0 1 * * *",
    )
    assert "dev.vnmaster.daily" in out
    assert "<string>--daily</string>" in out
    assert "<key>Weekday</key>" not in out
    assert "<integer>1</integer>" in out  # hour


def test_install_daily_plist_writes_file(tmp_path) -> None:
    from vnmaster.launchd import install_daily_plist
    p = install_daily_plist(daily_text="<plist/>", launchagents_dir=tmp_path)
    assert p.name == "dev.vnmaster.daily.plist"
    assert p.read_text() == "<plist/>"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_launchd.py -k "daily or wildcard" -v`
Expected: FAIL (cannot import render_daily_plist; weekday assertion).

- [ ] **Step 3: Update the cron parser to allow `*` weekday**

In `src/vnmaster/launchd.py`, change `SimpleCron.weekday` to allow None and update the regex + parser:

```python
@dataclass(frozen=True)
class SimpleCron:
    minute: int
    hour: int
    weekday: int | None  # None = every day (weekday field was '*')


_CRON_RE = re.compile(
    r"^(?P<minute>\d+)\s+(?P<hour>\d+)\s+\*\s+\*\s+(?P<weekday>[A-Z]{3}|\*)$"
)


def parse_simple_cron(expr: str) -> SimpleCron:
    m = _CRON_RE.match(expr.strip())
    if not m:
        raise InvalidCronError(
            "vnmaster supports only `M H * * DAY` cron expressions where DAY is "
            "SUN-SAT or * (every day)"
        )
    wd_token = m.group("weekday")
    if wd_token == "*":
        weekday: int | None = None
    else:
        weekday = _WEEKDAY_MAP.get(wd_token)
        if weekday is None:
            raise InvalidCronError(f"unknown weekday: {wd_token!r}")
    return SimpleCron(
        minute=int(m.group("minute")),
        hour=int(m.group("hour")),
        weekday=weekday,
    )
```

- [ ] **Step 4: Add the daily plist template + render + install**

In `src/vnmaster/launchd.py`, after `_BOT_PLIST_TEMPLATE` add:

```python
_DAILY_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.vnmaster.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{VNMASTER_BIN}}</string>
    <string>digest</string>
    <string>--daily</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{{HOUR}}</integer>
    <key>Minute</key>
    <integer>{{MINUTE}}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{{LOG_DIR}}/daily.out.log</string>
  <key>StandardErrorPath</key>
  <string>{{LOG_DIR}}/daily.err.log</string>
</dict>
</plist>
"""
```

After `render_bot_plist` add:

```python
def render_daily_plist(*, bin_path: Path, log_dir: Path, cron: str) -> str:
    parsed = parse_simple_cron(cron)
    return (
        _DAILY_PLIST_TEMPLATE
        .replace("{{VNMASTER_BIN}}", str(bin_path))
        .replace("{{LOG_DIR}}", str(log_dir))
        .replace("{{HOUR}}", str(parsed.hour))
        .replace("{{MINUTE}}", str(parsed.minute))
    )
```

After `install_plists` add:

```python
def install_daily_plist(*, daily_text: str, launchagents_dir: Path) -> Path:
    launchagents_dir.mkdir(parents=True, exist_ok=True)
    daily = launchagents_dir / "dev.vnmaster.daily.plist"
    daily.write_text(daily_text)
    return daily
```

- [ ] **Step 5: Add `daily_cron` to config**

In `src/vnmaster/config.py`, replace the `ScheduleConfig` class (lines 48-50) with:

```python
class ScheduleConfig(BaseModel):
    cron: str = "0 9 * * SAT"
    daily_cron: str = "0 1 * * *"
```

- [ ] **Step 6: Add a config test**

Add to `tests/unit/test_config.py`:

```python
def test_schedule_config_daily_cron_default() -> None:
    from vnmaster.config import ScheduleConfig
    assert ScheduleConfig().daily_cron == "0 1 * * *"
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/unit/test_launchd.py tests/unit/test_config.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/vnmaster/launchd.py src/vnmaster/config.py tests/unit/test_launchd.py tests/unit/test_config.py
git commit -m "feat(launchd): add daily plist, wildcard-weekday cron, daily_cron config"
```

---

### Task 3: Daily candidate selection

**Files:**
- Modify: `src/vnmaster/digest/select.py`
- Test: `tests/unit/test_digest_select.py`

**Interfaces:**
- Consumes: `LibraryGame` rows; F95Checker rows (objects with `.id`, `.version`, `.status`, `.last_updated`, `.changelog`, `.developer`, `.image_url`, `.tags`), passed as `f95_rows`.
- Produces: `select_daily_candidates(*, engine: Engine, f95_rows: list, now_epoch: int) -> DigestCandidates`. Fires per game when (not hidden) and (`acknowledged_version != live_version`) and (version signal OR status signal). Version signal: `is_user_behind(installed, live_version)` and `last_daily_notified_version != live_version`. Status signal: `status_changed(last_daily_notified_status, live_status)`. Returns `SelectedUpdate` objects carrying the live upstream version/status/changelog.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_digest_select.py` (top-of-file imports need `select_daily_candidates` and a small F95 stand-in):

```python
from types import SimpleNamespace

from vnmaster.digest.select import select_daily_candidates


def _f95(thread_id=42, version="0.7.0", status="1", changelog="notes",
         last_updated=200):
    return SimpleNamespace(
        id=thread_id, version=version, status=status, last_updated=last_updated,
        changelog=changelog, developer="Caribdis", image_url=None,
        tags=["corruption"],
    )


def test_daily_fires_on_new_version_user_behind(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=42, installed_version="0.5.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.5.0",
                      last_daily_notified_status="1")
    result = select_daily_candidates(
        engine=engine, f95_rows=[_f95(42, version="0.7.0")], now_epoch=500,
    )
    assert {u.f95_thread_id for u in result.updates} == {42}


def test_daily_silent_when_already_notified_for_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=42, installed_version="0.5.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.7.0",
                      last_daily_notified_status="1")
    result = select_daily_candidates(
        engine=engine, f95_rows=[_f95(42, version="0.7.0")], now_epoch=500,
    )
    assert result.updates == []


def test_daily_fires_on_status_change_even_if_current(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=42, installed_version="0.7.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.7.0",
                      last_daily_notified_status="1")  # was ongoing
    result = select_daily_candidates(
        engine=engine, f95_rows=[_f95(42, version="0.7.0", status="2")],  # now completed
        now_epoch=500,
    )
    updates = result.updates
    assert {u.f95_thread_id for u in updates} == {42}
    assert updates[0].status == "2"


def test_daily_respects_hidden_and_ack(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=1, installed_version="0.5.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.5.0", hidden=1)
        _seed_library(s, thread_id=2, installed_version="0.5.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.5.0",
                      acknowledged_version="0.7.0")
    result = select_daily_candidates(
        engine=engine,
        f95_rows=[_f95(1, version="0.7.0"), _f95(2, version="0.7.0")],
        now_epoch=500,
    )
    assert result.updates == []
```

Note: `_seed_library` already accepts `**overrides`, so `last_daily_notified_version` / `last_daily_notified_status` pass straight through to the `LibraryGame` constructor.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_digest_select.py -k daily -v`
Expected: FAIL (cannot import name 'select_daily_candidates').

- [ ] **Step 3: Implement `select_daily_candidates`**

In `src/vnmaster/digest/select.py`, add imports at the top (after the existing imports):

```python
import json

from vnmaster.status import status_changed
```

The file already imports `is_user_behind` from `vnmaster.magnitude`. Add the function at the end of the file:

```python
def select_daily_candidates(
    *,
    engine: Engine,
    f95_rows: list,
    now_epoch: int,
) -> DigestCandidates:
    """Select games for a nightly alert.

    Fires the first time a tracked game reaches a new upstream version the user
    is behind on, or the first time its notable status changes — each deduped
    against a per-game watermark (`last_daily_notified_version` /
    `last_daily_notified_status`) so a given change alerts once, not every night.

    Live upstream version/status/changelog come from `f95_rows` so this never
    depends on daily-mode upserts writing status.
    """
    by_id = {f.id: f for f in f95_rows}
    updates: list[SelectedUpdate] = []

    with session_scope(engine) as s:
        for g in s.execute(select(LibraryGame)).scalars().all():
            if g.hidden:
                continue
            f = by_id.get(g.f95_thread_id)
            if f is None:
                continue
            live_version = f.version
            live_status = f.status
            if (
                g.acknowledged_version is not None
                and g.acknowledged_version == live_version
            ):
                continue

            version_signal = (
                live_version is not None
                and is_user_behind(g.installed_version, live_version)
                and g.last_daily_notified_version != live_version
            )
            status_signal = status_changed(g.last_daily_notified_status, live_status)
            if not (version_signal or status_signal):
                continue

            updates.append(
                SelectedUpdate(
                    f95_thread_id=g.f95_thread_id,
                    game_title=g.game_title,
                    installed_version=g.installed_version,
                    latest_upstream_version=live_version,
                    upstream_last_updated_at=f.last_updated,
                    raw_changelog=f.changelog,
                    developer=f.developer,
                    image_url=f.image_url,
                    upstream_thread_url=(
                        f"https://f95zone.to/threads/.{g.f95_thread_id}/"
                    ),
                    last_played_at=g.last_played_at,
                    install_path=g.install_path,
                    tags_json=json.dumps(f.tags) if f.tags is not None else g.tags_json,
                    status=live_status,
                    status_changed=status_signal,
                )
            )

    return DigestCandidates(updates=updates)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_digest_select.py -v`
Expected: PASS (both the new daily tests and the existing weekly tests).

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/digest/select.py tests/unit/test_digest_select.py
git commit -m "feat(digest): add daily candidate selection with per-version/status watermark"
```

---

### Task 4: Pipeline daily mode

**Files:**
- Modify: `src/vnmaster/pipeline.py` (`PipelineDeps`, `run_digest_pipeline`, `_upsert_library`, `_previous_run_at`)
- Test: `tests/integration/test_pipeline_e2e.py` (new tests)

**Interfaces:**
- Consumes: `select_daily_candidates` (Task 3); `LibraryGame.last_daily_notified_version/status`, `DigestRun.kind` (Task 1).
- Produces: `PipelineDeps.mode: str = "weekly"`. When `mode="daily"`: selects via `select_daily_candidates`; returns without posting if there are no candidates; `_upsert_library(..., write_status=False)`; records `DigestRun(kind="daily")` + entries; sets `last_daily_notified_version`/`last_daily_notified_status` on alerted rows; never stamps `last_seen_in_digest_at`. `_upsert_library` gains `write_status: bool = True` and, on insert, baselines the two daily columns from the live F95 version/status. `_previous_run_at` counts only `kind="weekly"` runs.

- [ ] **Step 1: Write failing tests**

Add to `tests/integration/test_pipeline_e2e.py`. These reuse the module's existing mock style; a small helper builds deps for both modes.

```python
@pytest.mark.asyncio
async def test_daily_mode_silent_when_no_new_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    # Seed one game already at its notified version → no daily signal.
    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.7.0",
            latest_upstream_version="0.7.0", last_daily_notified_version="0.7.0",
            last_daily_notified_status="1", status="1",
            created_at=1, updated_at=1,
        ))

    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.iter_all_games = MagicMock(return_value=iter([
        SimpleNamespace(
            id=42, name="Eternum", version="0.7.0", developer="Caribdis",
            engine="Ren'Py", status="1", last_updated=100, changelog="notes",
            description="", image_url=None, tags=[], executable=None,
            archived=0, rating=4,
        )
    ]))
    llm_cache = MagicMock()
    webhook = AsyncMock()
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=llm_cache, webhook=webhook, bot=bot, config=cfg,
        now_epoch=1000, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    assert webhook.send.await_count == 0          # nothing posted
    assert llm_cache.extract_for.call_count == 0  # no LLM calls
    with session_scope(engine) as s:
        assert s.execute(select(DigestRun)).scalars().all() == []


@pytest.mark.asyncio
async def test_daily_mode_alerts_and_sets_watermark_without_touching_weekly_state(
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
            last_seen_in_digest_at=None, created_at=1, updated_at=1,
        ))

    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.iter_all_games = MagicMock(return_value=iter([
        SimpleNamespace(
            id=42, name="Eternum", version="0.7.0", developer="Caribdis",
            engine="Ren'Py", status="1", last_updated=300, changelog="v0.7 notes",
            description="", image_url=None, tags=[], executable=None,
            archived=0, rating=4,
        )
    ]))
    from vnmaster.llm.changelog import ExtractionResult
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
        now_epoch=1000, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    # Kickoff mentions @everyone.
    first_call = webhook.send.call_args_list[0]
    assert "@everyone" in (first_call.kwargs.get("content") or "")

    with session_scope(engine) as s:
        g = s.get(LibraryGame, 42)
        assert g.last_daily_notified_version == "0.7.0"   # watermark advanced
        assert g.last_seen_in_digest_at is None            # weekly throttle untouched
        runs = s.execute(select(DigestRun)).scalars().all()
        assert len(runs) == 1 and runs[0].kind == "daily"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_pipeline_e2e.py -k daily -v`
Expected: FAIL (`PipelineDeps` has no field `mode`).

- [ ] **Step 3: Add `mode` to `PipelineDeps`**

In `src/vnmaster/pipeline.py`, in the `PipelineDeps` dataclass, after `force: bool = False` add:

```python
    mode: str = "weekly"  # "weekly" or "daily"
```

- [ ] **Step 4: Import the daily selector**

In `src/vnmaster/pipeline.py`, update the select import (currently `from vnmaster.digest.select import select_digest_candidates`) to:

```python
from vnmaster.digest.select import select_daily_candidates, select_digest_candidates
```

- [ ] **Step 5: Branch selection + early-exit + kickoff + record by mode**

In `run_digest_pipeline`, replace step 5 (the `select_digest_candidates` block, currently lines 81-87) with:

```python
    # 5. Select candidates. Daily mode uses the per-version/status watermark
    #    and stays silent when there's nothing new; weekly uses its throttle.
    if deps.mode == "daily":
        candidates = select_daily_candidates(
            engine=deps.engine, f95_rows=f95_rows, now_epoch=deps.now_epoch,
        )
        if not candidates.updates:
            log.info("daily check: no new updates; nothing to post")
            return
    else:
        previous = _previous_run_at(deps.engine)
        candidates = select_digest_candidates(
            engine=deps.engine, previous_digest_run_at=previous,
            now_epoch=deps.now_epoch, max_repeat_weeks=4,
            force_all_behind=deps.force,
        )
```

Replace the kickoff block (step 7, currently lines 117-120) with:

```python
    if deps.mode == "daily":
        n = len(update_embeds)
        kickoff = (
            f"@everyone New update{'s' if n != 1 else ''} detected "
            f"— {n} game{'s' if n != 1 else ''} behind"
        )
    elif update_embeds:
        kickoff = f"Weekly digest — {len(update_embeds)} library updates"
    else:
        kickoff = "Weekly digest — no tracked games have updates this week."
```

Change the `_upsert_library` call (step 4, currently line 79) to pass the mode's write policy:

```python
    _upsert_library(
        deps.engine, match_result, f95_rows, deps.now_epoch,
        write_status=(deps.mode != "daily"),
    )
```

Replace the record block (step 8, currently lines 126-153) with:

```python
    # 8. Record run + entries. Daily runs are tagged so they never move the
    #    weekly "since last digest" pointer, and they advance the daily
    #    watermark instead of stamping the weekly last-seen throttle.
    notified = {u.f95_thread_id: u for u in candidates.updates}
    with session_scope(deps.engine) as s:
        run = DigestRun(
            run_at=deps.now_epoch,
            updates_count=len(update_embeds),
            llm_calls=llm_calls,
            llm_cost_usd=llm_cost,
            kind=deps.mode,
        )
        s.add(run)
        s.flush()
        for message_id, embed_index, kind, thread_id in posted.entries:
            s.add(DigestEntry(
                run_id=run.id, discord_message_id=message_id,
                embed_index=embed_index, kind=kind, f95_thread_id=thread_id,
            ))
            if kind != "update":
                continue
            row = s.execute(
                select(LibraryGame).where(LibraryGame.f95_thread_id == thread_id)
            ).scalar_one_or_none()
            if row is None:
                log.warning(
                    "library_games row missing for thread %d after digest post",
                    thread_id,
                )
                continue
            if deps.mode == "daily":
                u = notified.get(thread_id)
                if u is not None:
                    row.last_daily_notified_version = u.latest_upstream_version
                    row.last_daily_notified_status = u.status
            else:
                row.last_seen_in_digest_at = deps.now_epoch
```

- [ ] **Step 6: Add `write_status` + daily baseline to `_upsert_library`**

In `src/vnmaster/pipeline.py`, change the `_upsert_library` signature (currently line 214) to:

```python
def _upsert_library(engine, match_result, f95_rows, now_epoch: int, write_status: bool = True) -> None:
```

In the **matched** branch, after `data = dict(...)` is built (currently ends line 248) and before the `if existing is None:` check (line 249), add:

```python
            if not write_status:
                data.pop("status", None)
                data.pop("status_changed", None)
```

Change the matched insert (currently lines 250-252) to baseline the daily columns:

```python
            if existing is None:
                s.add(LibraryGame(
                    f95_thread_id=m.f95_thread_id, created_at=now_epoch,
                    last_daily_notified_version=(f95.version if f95 else None),
                    last_daily_notified_status=(f95.status if f95 else None),
                    **data,
                ))
```

In the **unmatched f95** branch, after its `data = dict(...)` (currently ends line 279) and before its `if existing is None:` (line 280), add:

```python
            if not write_status:
                data.pop("status", None)
                data.pop("status_changed", None)
```

Change the unmatched insert (currently lines 280-283) to:

```python
            if existing is None:
                s.add(LibraryGame(
                    f95_thread_id=f95.id, created_at=now_epoch,
                    last_daily_notified_version=f95.version,
                    last_daily_notified_status=f95.status,
                    **data,
                ))
```

- [ ] **Step 7: Filter `_previous_run_at` to weekly runs**

In `src/vnmaster/pipeline.py`, replace the query in `_previous_run_at` (currently lines 291-293) with:

```python
        last = s.execute(
            select(DigestRun)
            .where(DigestRun.kind == "weekly")
            .order_by(DigestRun.run_at.desc())
            .limit(1)
        ).scalar_one_or_none()
```

- [ ] **Step 8: Run the pipeline tests**

Run: `uv run pytest tests/integration/test_pipeline_e2e.py tests/unit/test_pipeline_delta.py tests/unit/test_pipeline_persist.py -v`
Expected: PASS (new daily tests + existing weekly tests unchanged).

- [ ] **Step 9: Commit**

```bash
git add src/vnmaster/pipeline.py tests/integration/test_pipeline_e2e.py
git commit -m "feat(pipeline): add daily mode with silent-when-empty and isolated watermark"
```

---

### Task 5: CLI `--daily` flag, webhook mentions, scheduler install

**Files:**
- Modify: `src/vnmaster/cli.py` (digest command `--daily`, `_WebhookAdapter`, `install-scheduler`)
- Modify: `src/vnmaster/init_wizard.py` (install daily plist, config default)
- Test: `tests/unit/test_cli.py`, `tests/unit/test_init_wizard.py`

**Interfaces:**
- Consumes: `PipelineDeps.mode` (Task 4); `render_daily_plist`, `install_daily_plist` (Task 2); `cfg.schedule.daily_cron`.
- Produces: `vnmaster digest --daily` sets `mode="daily"`; the webhook adapter passes `allowed_mentions` so `@everyone` in daily kickoff content actually pings; `install-scheduler` and the wizard render and install `dev.vnmaster.daily.plist`.

- [ ] **Step 1: Write a failing CLI test for the `--daily` flag**

Add this concrete, dependency-free test to `tests/unit/test_cli.py`. It asserts the Click option exists, is a flag, and defaults to off (the full `digest` command wires discord + anthropic clients, so a flag-shape test is the reliable unit here):

```python
def test_digest_has_daily_flag():
    from vnmaster.cli import digest
    opt = {p.name: p for p in digest.params}["daily"]
    assert opt.is_flag and opt.default is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli.py -k daily -v`
Expected: FAIL (no such option / param `daily`).

- [ ] **Step 3: Add the `--daily` flag and thread `mode` through**

In `src/vnmaster/cli.py`, add an option to the `digest` command (after the `--force` option, before `def digest(...)`, currently around line 43):

```python
@click.option(
    "--daily", "daily", is_flag=True, default=False,
    help="Nightly early-warning mode: alert only on newly-detected updates or "
         "status changes, post nothing when there's nothing new, and leave the "
         "weekly digest's state untouched.",
)
```

Update the function signature to `def digest(config_path: Path | None, force: bool, daily: bool) -> None:` and, in the `PipelineDeps(...)` construction (currently lines 115-121), add:

```python
        mode="daily" if daily else "weekly",
```

- [ ] **Step 4: Make the webhook adapter allow @everyone**

In `src/vnmaster/cli.py`, inside `_WebhookAdapter.send` (currently lines 90-97), set allowed mentions so a daily `@everyone` in the content pings. Replace the method body with:

```python
        async def send(self, *args, **kwargs):
            embeds = kwargs.pop("embeds", None)
            if embeds is not None:
                kwargs["embeds"] = [
                    discord.Embed.from_dict(e) if isinstance(e, dict) else e
                    for e in embeds
                ]
            # Let @everyone in the daily kickoff actually deliver a ping. Weekly
            # content never contains @everyone, so this is a no-op there.
            kwargs.setdefault(
                "allowed_mentions", discord.AllowedMentions(everyone=True)
            )
            return self._w.send(*args, **kwargs, wait=True)
```

- [ ] **Step 5: Install the daily plist in `install-scheduler`**

In `src/vnmaster/cli.py`, in `install_scheduler`, update the launchd import (currently lines 254-256) to include the daily helpers:

```python
    from vnmaster.launchd import (
        install_daily_plist, install_plists, render_bot_plist,
        render_daily_plist, render_weekly_plist,
    )
```

After the `weekly_path, bot_path = install_plists(...)` call (currently lines 267-269), add:

```python
    daily = render_daily_plist(
        bin_path=bin_path, log_dir=paths.log_dir, cron=cfg.schedule.daily_cron
    )
    daily_path = install_daily_plist(daily_text=daily, launchagents_dir=launchagents)
```

Update the printed output to include the daily plist. Replace the `click.echo(f"Installed:\n ...")` block and the bootstrap instructions (currently lines 270-279) with:

```python
    click.echo(f"Installed:\n  {weekly_path}\n  {daily_path}\n  {bot_path}\n")
    click.echo(
        "Load them with the modern launchctl syntax (NOT `launchctl load`,\n"
        "which is deprecated and gives cryptic I/O errors):\n\n"
        f"  launchctl bootstrap gui/$(id -u) {bot_path}\n"
        f"  launchctl bootstrap gui/$(id -u) {weekly_path}\n"
        f"  launchctl bootstrap gui/$(id -u) {daily_path}\n\n"
        "To stop/remove later:\n"
        f"  launchctl bootout gui/$(id -u) {bot_path}\n"
        f"  launchctl bootout gui/$(id -u) {weekly_path}\n"
        f"  launchctl bootout gui/$(id -u) {daily_path}"
    )
```

- [ ] **Step 6: Install the daily plist in the wizard + config default**

In `src/vnmaster/init_wizard.py`, update the launchd import in step 8 (currently lines 382-384) to:

```python
    from vnmaster.launchd import (
        install_daily_plist, install_plists, render_bot_plist,
        render_daily_plist, render_weekly_plist,
    )
```

After the `weekly_path, bot_path = install_plists(...)` call (currently lines 393-395), add:

```python
    daily = render_daily_plist(
        bin_path=bin_path, log_dir=paths.log_dir, cron="0 1 * * *"
    )
    daily_path = install_daily_plist(daily_text=daily, launchagents_dir=launchagents)
```

Update the confirmation echo (currently line 396) to:

```python
    click.echo(f"Installed launchd plists: {weekly_path}, {daily_path}, {bot_path}")
```

Update the schedule default in the config writer (currently line 637) to include `daily_cron`:

```python
        "schedule": existing_config.get("schedule") or {"cron": "0 9 * * SAT", "daily_cron": "0 1 * * *"},
```

- [ ] **Step 7: Add a wizard/scheduler test**

Add to `tests/unit/test_init_wizard.py` (or `tests/unit/test_launchd.py` if the wizard test file mocks too much) a focused check that the daily plist is produced with the right label. If `test_init_wizard.py` already has a scheduler test, extend it; otherwise add:

```python
def test_daily_plist_renders_for_wizard_default() -> None:
    from pathlib import Path
    from vnmaster.launchd import render_daily_plist
    out = render_daily_plist(
        bin_path=Path("/opt/bin/vnmaster"), log_dir=Path("/L"), cron="0 1 * * *"
    )
    assert "dev.vnmaster.daily" in out
    assert "--daily" in out
```

- [ ] **Step 8: Run CLI + wizard + launchd tests**

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_init_wizard.py tests/unit/test_launchd.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/vnmaster/cli.py src/vnmaster/init_wizard.py tests/unit/test_cli.py tests/unit/test_init_wizard.py tests/unit/test_launchd.py
git commit -m "feat(cli): add digest --daily, everyone mentions, and daily scheduler install"
```

---

### Task 6: Full suite + lint + deploy

**Files:** none (verification + operational).

- [ ] **Step 1: Run the whole test suite**

Run: `uv run pytest -q`
Expected: all pass. Investigate any weekly-path test that changed behavior — weekly output must be identical.

- [ ] **Step 2: Lint + type-check**

Run: `uv run ruff check src tests && uv run mypy src`
Expected: clean (match the repo's current baseline; fix anything new this change introduced).

- [ ] **Step 3: Reinstall the tool (deploy)**

Run: `uv tool install . --force`
This rebuilds the `vnmaster` entrypoint from source. `ensure_schema` self-heals the live `vnmaster.db` (adds + backfills the new columns) on the next run, so the daily baseline is set from the versions the weekly digest already pushed.

- [ ] **Step 4: Install + load the daily launchd job**

Run: `vnmaster install-scheduler --config ~/.config/vnmaster/config.toml`
Then load only the newly added daily job (the bot + weekly are already loaded):

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.vnmaster.daily.plist
```

- [ ] **Step 5: Smoke-test the daily run by hand**

Run: `vnmaster digest --daily --config ~/.config/vnmaster/config.toml`
Expected: `digest run complete`, and because the backlog was baselined, no Discord post (a quiet night). Confirm with `tail -n 20 ~/Library/Logs/VNMaster/daily.out.log` showing "no new updates" or, if a genuine update landed, one `@everyone` post.

- [ ] **Step 6: Add `schedule.daily_cron` to the live config (optional)**

If the user's `~/.config/vnmaster/config.toml` has a `[schedule]` section without `daily_cron`, add `daily_cron = "0 1 * * *"`. Not required (the default applies), but makes the setting visible/editable.

---

## Notes for the implementer

- The weekly path is the safety-critical invariant. After Task 4, re-read `select_digest_candidates`, `_previous_run_at`, and the weekly record path and confirm daily runs write none of: `last_seen_in_digest_at`, `status`, `status_changed`, a `kind="weekly"` DigestRun.
- `status` values from F95Checker are integers stored as strings (e.g. `"2"` = Completed); `status.status_changed()` int-coerces via `status_label`, so comparing the stored watermark string to the live value works.
- Daily makes zero LLM calls on a quiet night because the early-return happens before the extraction loop.
- The spec described the `@everyone` ping as a `DiscordPoster.mention_everyone` flag. This plan implements it more simply: the pipeline puts `@everyone` in the daily kickoff text, and the CLI's `_WebhookAdapter` sets `allowed_mentions` so the ping delivers. `DiscordPoster` is unchanged — don't add a `mention_everyone` param.

from pathlib import Path

import pytest

from vnmaster.db.ro_f95checker import F95CheckerGame
from vnmaster.matcher import match_library, version_tokens
from vnmaster.scanners.types import InstalledGame, PlayHistoryEntry


def _save(
    name: str,
    last_played: int = 1700_000_000,
    version: str | None = None,
) -> PlayHistoryEntry:
    return PlayHistoryEntry(
        save_dir_name=name,
        save_dir_path=Path("/tmp") / name,
        last_played_at=last_played,
        first_played_at=last_played - 100,
        save_count=2,
        total_save_size_bytes=100,
        persistent_data_present=True,
        last_played_version=version,
    )


def _disk(name: str, version: str = "0.5.0", hint: str | None = None) -> InstalledGame:
    return InstalledGame(
        folder_name=name,
        install_path=Path("/tmp/Games") / name,
        installed_version=version,
        save_dir_hint=hint,
        disk_size_bytes=1_000_000,
        launcher_name="MyGame.app",
    )


def _f95(
    thread_id: int, name: str, executable: str | None = None,
    version: str = "0.7.0",
) -> F95CheckerGame:
    return F95CheckerGame(
        id=thread_id, name=name, version=version, developer="Dev", engine="Ren'Py",
        status="ongoing", last_updated=1700_000_001, changelog="", description="",
        image_url="", tags=["corruption"], executable=executable, archived=0, rating=None,
    )


def test_disk_matches_by_executable_path() -> None:
    install_path = Path("/Users/x/Games/Eternum")
    disk_entry = _disk("Eternum-0.5.2-pc")
    disk_entry = disk_entry.model_copy(update={"install_path": install_path})
    f95 = _f95(42, "Eternum", executable=str(install_path / "MyGame.app"))
    result = match_library(
        play_history=[],
        installed=[disk_entry],
        f95_rows=[f95],
        cached_pairings={},
        fuzzy_threshold=85,
    )
    assert result.matches[0].f95_thread_id == 42
    assert result.matches[0].install_path == install_path


def test_disk_falls_back_to_fuzzy_match_on_name() -> None:
    disk_entry = _disk("Eternum-0.5.2-pc")
    f95 = _f95(42, "Eternum")
    result = match_library(
        play_history=[], installed=[disk_entry], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=85,
    )
    assert len(result.matches) == 1
    assert result.matches[0].f95_thread_id == 42


def test_save_dir_fuzzy_matches_when_disk_hint_missing() -> None:
    save = _save("Eternum-1234567890")
    f95 = _f95(42, "Eternum")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=85,
    )
    assert result.matches[0].f95_thread_id == 42
    assert result.matches[0].save_dir_name == "Eternum-1234567890"


def test_disk_save_dir_hint_binds_to_play_history() -> None:
    save = _save("SecretSave-1234567890")
    disk_entry = _disk("MyGame", hint="SecretSave-1234567890")
    f95 = _f95(42, "MyGame")
    result = match_library(
        play_history=[save], installed=[disk_entry], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=85,
    )
    m = result.matches[0]
    assert m.f95_thread_id == 42
    assert m.save_dir_name == "SecretSave-1234567890"
    assert m.installed_version == "0.5.0"


def test_unmatched_when_below_threshold() -> None:
    disk_entry = _disk("CompletelyDifferentName")
    f95 = _f95(42, "Eternum")
    result = match_library(
        play_history=[], installed=[disk_entry], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=85,
    )
    assert result.matches == []
    assert len(result.unmatched_installed) == 1


def test_save_dir_with_timestamp_suffix_matches_clean_f95_name() -> None:
    """Regression: 'Eternum-1610153667' vs 'Eternum' raw-WRatios at 90,
    but 'ATouchofMagic-1691411418' vs 'A Touch of Magic' only scores 81 —
    below the default 90 threshold — because the long numeric suffix
    drags it down. Cleaning the save_dir_name first should bring both
    to score 100."""
    save = _save("ATouchofMagic-1691411418")
    f95 = _f95(42, "A Touch of Magic")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=85,
    )
    assert len(result.matches) == 1
    assert result.matches[0].f95_thread_id == 42
    assert result.matches[0].save_dir_name == "ATouchofMagic-1691411418"


def test_nested_save_dir_matches_on_its_last_path_segment() -> None:
    """A path-valued config.save_directory ("Talothral/Sorcerer2") should match
    on the leaf. Keeping the parent in the string scores 85.3 against
    "Sorcerer 2" — barely over the threshold — while the leaf alone scores 94.7.
    """
    save = _save("Talothral/Sorcerer2")
    f95 = _f95(287738, "Sorcerer 2")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=90,
    )
    assert len(result.matches) == 1
    assert result.matches[0].f95_thread_id == 287738
    # The full relative path stays the identity, so pairings remain unambiguous.
    assert result.matches[0].save_dir_name == "Talothral/Sorcerer2"


def test_save_dir_with_dev_suffix_matches_clean_f95_name() -> None:
    """Dev-added suffixes like 'Saves' / 'Week2' should also be stripped
    before fuzzy matching."""
    pairs = [
        ("FetishLocator-Week2", "Fetish Locator"),
        ("ChasingSunsets-1590115247", "Chasing Sunsets"),
    ]
    for save_dir, f95_name in pairs:
        save = _save(save_dir)
        f95 = _f95(42, f95_name)
        result = match_library(
            play_history=[save], installed=[], f95_rows=[f95],
            cached_pairings={}, fuzzy_threshold=85,
        )
        assert len(result.matches) == 1, (
            f"expected match for {save_dir!r} vs {f95_name!r}, "
            f"got {[(m.save_dir_name, m.game_title) for m in result.matches]}"
        )


def test_cached_pairing_overrides_fuzzy_match() -> None:
    save = _save("BoringSaveName")
    f95_a = _f95(10, "Game A")
    f95_b = _f95(20, "Game B")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95_a, f95_b],
        cached_pairings={"BoringSaveName": 20}, fuzzy_threshold=85,
    )
    assert result.matches[0].f95_thread_id == 20


# ---------------------------------------------------------------------------
# version_tokens tests (Deliverable 1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("s,expected", [
    ("0.4RC1", {"0.4"}),
    ("Ch.4 v0.4", {"0.4"}),
    ("v0.20.3.1", {"0.20.3.1"}),
    ("Episode-10", set()),
    ("7.0", {"7.0"}),
    ("", set()),
    (None, set()),
])
def test_version_tokens(s: str | None, expected: set[str]) -> None:
    assert version_tokens(s) == expected


# ---------------------------------------------------------------------------
# Corroborated matching tests (Deliverable 1)
# ---------------------------------------------------------------------------

def test_corroborated_match_desert_stalker() -> None:
    """DesertStalkerEA-100001 fuzzy ~89 (< 90), but version 0.20.3.1 matches."""
    save = _save("DesertStalkerEA-100001", version="0.20.3.1")
    f95 = _f95(101, "Desert Stalker", version="0.20.3.1")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=90,
    )
    assert len(result.matches) == 1
    assert result.matches[0].f95_thread_id == 101
    # Should have emitted a learned pairing with method "version"
    version_lp = [lp for lp in result.learned_pairings if lp.method == "version"]
    assert len(version_lp) == 1
    assert version_lp[0].f95_thread_id == 101
    assert version_lp[0].save_dir_name == "DesertStalkerEA-100001"
    assert version_lp[0].confidence == pytest.approx(0.85)


def test_corroborated_match_prince_of_suburbia() -> None:
    """PrinceOfSuburbiaRewrite-1628326737 fuzzy ~73 but version 1.3.5 matches."""
    save = _save("PrinceOfSuburbiaRewrite-1628326737", version="1.3.5")
    f95 = _f95(202, "Prince of Suburbia", version="Ch.5 v1.3.5")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=90,
    )
    assert len(result.matches) == 1
    assert result.matches[0].f95_thread_id == 202


def test_corroborated_no_false_positive() -> None:
    """Teste-1764891901 fuzzy ~60 AND no version intersection → unmatched."""
    save = _save("Teste-1764891901", version="7.0")
    f95 = _f95(101, "Desert Stalker", version="0.20.3.1")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=90,
    )
    assert result.matches == []
    assert len(result.unmatched_play_history) == 1


def test_name_match_still_works_and_emits_learned_pairing() -> None:
    """WRatio >= 90 → method 'name', confidence 0.95, learned pairing emitted."""
    save = _save("Eternum-1234567890", version="0.9.0")
    f95 = _f95(42, "Eternum", version="0.9.0")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=90,
    )
    assert len(result.matches) == 1
    assert result.matches[0].f95_thread_id == 42
    name_lp = [lp for lp in result.learned_pairings if lp.method == "name"]
    assert len(name_lp) == 1
    assert name_lp[0].f95_thread_id == 42
    assert name_lp[0].confidence == pytest.approx(0.95)


def test_multiple_part_installs_aggregate_to_one_entry() -> None:
    """A multi-part install (Part 1, Part 2 dirs under a shared version root)
    should merge into a single library entry: sizes summed, install_path
    collapsed to the shared version root, version kept from the first part.
    """
    version_root = Path("/g/Split Game/v2.0")
    part1 = _disk("Split Game Part 1").model_copy(
        update={"install_path": version_root / "Part 1", "disk_size_bytes": 100}
    )
    part2 = _disk("Split Game Part 2").model_copy(
        update={"install_path": version_root / "Part 2", "disk_size_bytes": 250}
    )
    f95 = _f95(55, "Split Game")
    result = match_library(
        play_history=[], installed=[part1, part2], f95_rows=[f95],
        cached_pairings={}, fuzzy_threshold=85,
    )
    matches = [m for m in result.matches if m.f95_thread_id == 55]
    assert len(matches) == 1
    assert matches[0].disk_size_bytes == 350
    assert matches[0].install_path == version_root
    assert matches[0].installed_version == "0.5.0"


def test_cached_pairing_does_not_produce_learned_pairing() -> None:
    """Matches that come from cached_pairings must NOT appear in learned_pairings."""
    save = _save("BoringSaveName")
    f95_b = _f95(20, "Game B")
    result = match_library(
        play_history=[save], installed=[], f95_rows=[f95_b],
        cached_pairings={"BoringSaveName": 20}, fuzzy_threshold=85,
    )
    assert result.matches[0].f95_thread_id == 20
    # No learned pairings — the match came from the cache.
    assert result.learned_pairings == []

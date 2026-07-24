from vnmaster.status import status_callout, status_changed, status_int, status_label


def test_status_int_tolerates_str_and_none() -> None:
    assert status_int(2) == 2
    assert status_int("2") == 2          # DB round-trips ints as text
    assert status_int(None) is None
    assert status_int("ongoing") is None  # legacy/string statuses


def test_status_label_maps_only_notable() -> None:
    assert status_label(2) == "Completed"
    assert status_label(3) == "On Hold"
    assert status_label(4) == "Abandoned"
    assert status_label(1) == ""   # ongoing/normal — no tag
    assert status_label(5) == ""   # default sentinel
    assert status_label(None) == ""
    assert status_label("2") == "Completed"


def test_status_changed_only_on_notable_difference() -> None:
    assert status_changed(1, 2) is True    # ongoing -> completed
    assert status_changed(2, 4) is True    # completed -> abandoned
    assert status_changed(2, 1) is True    # completed -> ongoing (resumed)
    assert status_changed(1, 5) is False   # ongoing <-> unchecked (both "")
    assert status_changed("2", 2) is False  # str/int, same notable
    assert status_changed(2, 2) is False


def test_status_callout_text() -> None:
    assert status_callout(2) == "✅ Now completed"
    assert status_callout(3) == "⏸️ Now on hold"
    assert status_callout(4) == "🚫 Now abandoned"
    assert status_callout(1) == "▶️ Resumed"  # only used when status changed

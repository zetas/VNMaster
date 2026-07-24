from vnmaster.llm.budget import InMemoryBudget


def test_initial_remaining_equals_cap() -> None:
    b = InMemoryBudget(cap_usd=5.0)
    assert b.remaining_usd() == 5.0


def test_record_subtracts_from_remaining() -> None:
    b = InMemoryBudget(cap_usd=5.0)
    b.record(1.25)
    assert b.remaining_usd() == 3.75


def test_record_can_go_negative_but_remaining_clamps_to_zero() -> None:
    b = InMemoryBudget(cap_usd=1.0)
    b.record(1.5)
    assert b.remaining_usd() == 0.0


def test_reset_restores_full_cap() -> None:
    b = InMemoryBudget(cap_usd=5.0)
    b.record(2.0)
    b.reset()
    assert b.remaining_usd() == 5.0

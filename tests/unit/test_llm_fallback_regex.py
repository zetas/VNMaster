from vnmaster.llm.fallback_regex import extract_with_regex


def test_extracts_renders_animations_words() -> None:
    text = """
    v0.7.0
    - 800 new renders
    - 35 animations
    - 12,000 lines of dialogue
    """
    result = extract_with_regex(text)
    assert len(result["versions"]) == 1
    v = result["versions"][0]
    assert v["version"] == "0.7.0"
    assert v["renders"] == 800
    assert v["animations"] == 35
    assert v["words"] == 12000
    assert v["bugfix_only"] is False


def test_counts_images_as_renders() -> None:
    # Devs (e.g. Eternum) write "images" rather than "renders".
    text = "v0.9.0\n- 2050+ new images\n- 90+ new animations"
    v = extract_with_regex(text)["versions"][0]
    assert v["renders"] == 2050
    assert v["animations"] == 90


def test_marks_bugfix_only_when_no_content_changes() -> None:
    text = "v0.5.3\n- Bug fixes\n- Performance improvements"
    result = extract_with_regex(text)
    assert result["versions"][0]["bugfix_only"] is True


def test_handles_multiple_version_blocks() -> None:
    text = """
    v0.7.0
    - 800 renders
    - 35 animations

    v0.6.5
    - 200 renders
    """
    result = extract_with_regex(text)
    assert len(result["versions"]) == 2
    versions = {v["version"]: v for v in result["versions"]}
    assert versions["0.7.0"]["renders"] == 800
    assert versions["0.6.5"]["renders"] == 200


def test_empty_input_returns_empty_versions() -> None:
    assert extract_with_regex("") == {"versions": []}

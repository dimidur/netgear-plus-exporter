"""Tests for the shape of .github/release.yml.

A malformed file does not fail a release: GitHub falls back to uncategorised
notes, so the Breaking Changes heading the README tells upgraders to read
disappears without a word. These pin the shape the README's promise depends
on. Only the shape -- whether the labels match reality is a judgement no
assertion can make.
"""

from __future__ import annotations

import copy
import pathlib
from typing import Any

import pytest
import yaml

CONFIG = pathlib.Path(__file__).resolve().parent.parent / ".github" / "release.yml"
BREAKING_HEADING = "Breaking Changes"


def problems(cfg: Any) -> list[str]:
    """Every way this config would silently degrade the release notes."""
    found: list[str] = []

    if not isinstance(cfg, dict):
        return ["no changelog.categories"]
    changelog = cfg.get("changelog")
    categories = changelog.get("categories") if isinstance(changelog, dict) else None
    if not categories or not isinstance(categories, list):
        return ["no changelog.categories"]

    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            found.append(f"category {index} is not a mapping")
            continue
        title = category.get("title")
        if not title:
            found.append(f"category {index} has no title")
        own_labels = category.get("labels")
        if not isinstance(own_labels, list) or not own_labels:
            found.append(f"{title!r} needs a non-empty list of labels")

    if found:
        return found

    labels = [label for category in categories for label in category["labels"]]
    if labels.count("*") != 1:
        found.append("expected exactly one '*' catch-all")
    elif labels[-1] != "*":
        found.append(f"catch-all must be last, not {labels[-1]!r}")
    # A pull request falls into the first category it matches, so a
    # breaking-change category below Fixes would never claim a PR labelled
    # both -- and the README's promise quietly stops holding.
    if labels[0] != "breaking-change":
        found.append(f"breaking-change must come first, not {labels[0]!r}")
    # README and CONTRIBUTING send upgraders to a heading of this exact name,
    # so renaming it breaks the promise without failing anything else.
    heading = categories[0].get("title")
    if heading != BREAKING_HEADING:
        found.append(f"first heading must read {BREAKING_HEADING!r}, not {heading!r}")
    return found


def load() -> Any:
    return yaml.safe_load(CONFIG.read_text())


def test_the_shipped_config_is_well_formed():
    # Arrange
    cfg = load()

    # Act
    found = problems(cfg)

    # Assert
    assert found == []


def test_a_config_without_a_changelog_key_is_rejected():
    # Arrange / Act
    found = problems({"something": "else"})

    # Assert
    assert found == ["no changelog.categories"]


@pytest.mark.parametrize(
    "cfg",
    [
        None,
        {},
        {"changelog": None},
        {"changelog": {}},
        # A file written as a list or a bare scalar must report, not raise.
        [{"changelog": {}}],
        "changelog",
        {"changelog": "categories"},
    ],
)
def test_an_empty_or_malformed_config_is_rejected(cfg):
    # Arrange / Act
    found = problems(cfg)

    # Assert
    assert found == ["no changelog.categories"]


def test_a_misspelt_categories_key_is_rejected():
    # Arrange
    cfg = load()
    cfg["changelog"]["catagories"] = cfg["changelog"].pop("categories")

    # Act
    found = problems(cfg)

    # Assert
    assert found == ["no changelog.categories"]


def test_a_misspelt_title_is_rejected():
    # Arrange
    cfg = load()
    first = cfg["changelog"]["categories"][0]
    first["titel"] = first.pop("title")

    # Act
    found = problems(cfg)

    # Assert
    assert found == ["category 0 has no title"]


def test_a_singular_label_key_is_rejected():
    # Arrange
    cfg = load()
    fixes = cfg["changelog"]["categories"][1]
    fixes["label"] = fixes.pop("labels")

    # Act
    found = problems(cfg)

    # Assert
    assert found == ["'Fixes' needs a non-empty list of labels"]


def test_labels_written_as_a_bare_string_are_rejected():
    # A dropped "- " leaves a scalar, which iterates as characters rather
    # than failing.
    # Arrange
    cfg = load()
    cfg["changelog"]["categories"][1]["labels"] = "bug"

    # Act
    found = problems(cfg)

    # Assert
    assert found == ["'Fixes' needs a non-empty list of labels"]


def test_a_null_category_entry_is_rejected():
    # Arrange
    cfg = load()
    categories = cfg["changelog"]["categories"]
    categories.append(None)

    # Act
    found = problems(cfg)

    # Assert
    assert found == [f"category {len(categories) - 1} is not a mapping"]


def test_renaming_the_breaking_changes_heading_is_rejected():
    # The labels would still route correctly, so nothing else catches this.
    # Arrange
    cfg = load()
    cfg["changelog"]["categories"][0]["title"] = "Breaking"

    # Act
    found = problems(cfg)

    # Assert
    assert found == ["first heading must read 'Breaking Changes', not 'Breaking'"]


def test_losing_the_catch_all_is_rejected():
    # Arrange
    cfg = load()
    cfg["changelog"]["categories"][-1]["labels"] = ["chore"]

    # Act
    found = problems(cfg)

    # Assert
    assert found == ["expected exactly one '*' catch-all"]


def test_a_catch_all_that_is_not_last_is_rejected():
    # Everything after the catch-all is unreachable.
    # Arrange
    cfg = load()
    categories = cfg["changelog"]["categories"]
    categories.insert(0, categories.pop())

    # Act
    found = problems(cfg)

    # Assert
    assert "catch-all must be last, not 'dependencies'" in found


def test_deleting_the_breaking_changes_category_is_rejected():
    # Arrange
    cfg = load()
    cfg["changelog"]["categories"].pop(0)

    # Act
    found = problems(cfg)

    # Assert
    assert found == [
        "breaking-change must come first, not 'bug'",
        "first heading must read 'Breaking Changes', not 'Fixes'",
    ]


def test_demoting_breaking_changes_below_fixes_is_rejected():
    # The heading would still exist, but a PR labelled breaking-change AND
    # bug would render under Fixes.
    # Arrange
    cfg = load()
    categories = cfg["changelog"]["categories"]
    categories[0], categories[1] = categories[1], categories[0]

    # Act
    found = problems(cfg)

    # Assert
    assert found == [
        "breaking-change must come first, not 'bug'",
        "first heading must read 'Breaking Changes', not 'Fixes'",
    ]


def test_the_shipped_config_is_not_mutated_by_these_tests():
    # Arrange
    before = copy.deepcopy(load())

    # Act
    problems(load())

    # Assert
    assert load() == before

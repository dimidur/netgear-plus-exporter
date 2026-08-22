"""The module seams, asserted rather than described.

The rule worth enforcing is the outer one: a py-netgear-plus change must be a
one-module edit, so nothing except the device binding may import the library.
Modules are discovered rather than listed, because a rule with a hand-written
list of exceptions stops covering the next module someone adds.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pathlib
import pkgutil

import pytest

import netgear_plus_exporter

BINDING = "netgear_plus_exporter.upstream"
LIBRARY = "py_netgear_plus"


def _package_modules() -> list[str]:
    found = [
        f"netgear_plus_exporter.{info.name}"
        for info in pkgutil.iter_modules(netgear_plus_exporter.__path__)
    ]
    return ["netgear_plus_exporter", *found]


def _imports(name: str) -> set[str]:
    """Every module named by an import in `name`, by absolute dotted path.

    A relative import is resolved against the importing module's package, so
    `from . import upstream` and `import netgear_plus_exporter.upstream` both
    yield the same string. Without that the relative form, which is the one
    this package writes, would never match the rule's absolute target.
    """
    module = importlib.import_module(name)
    source = pathlib.Path(module.__file__ or "").read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            relative = "." * node.level + (node.module or "")
            base = importlib.util.resolve_name(relative, module.__package__)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _mentions(imports: set[str], target: str) -> bool:
    """True when any import is `target` or a module inside it."""
    return any(name == target or name.startswith(f"{target}.") for name in imports)


def test_the_binding_is_the_module_that_imports_the_library():
    # Act
    imports = _imports(BINDING)

    # Assert
    assert _mentions(imports, LIBRARY)


@pytest.mark.parametrize("name", [m for m in _package_modules() if m != BINDING])
def test_no_other_module_imports_the_library(name):
    """Discovered, so a module added later is covered without anyone
    remembering to edit a list here.
    """
    # Act
    imports = _imports(name)

    # Assert
    assert not _mentions(imports, LIBRARY), name


def test_metric_translation_does_not_import_the_binding():
    """The collector reaches the library only through the scraper's reading,
    never by name. Same matching as the rule above, and relative imports are
    resolved first, so neither `from . import upstream` nor
    `import netgear_plus_exporter.upstream` slips past.
    """
    # Act
    imports = _imports("netgear_plus_exporter.collector")

    # Assert
    assert not _mentions(imports, BINDING)


@pytest.mark.parametrize("name", _package_modules())
def test_no_module_grew_past_its_budget(name):
    """A proxy, not the rule. What forced the split was one module holding
    several reasons to change; nothing counts those automatically, so this
    catches the size that came with them and nothing else.
    """
    # Arrange
    module = importlib.import_module(name)

    # Act
    lines = pathlib.Path(module.__file__ or "").read_text(encoding="utf-8").splitlines()

    # Assert
    assert len(lines) < 400, f"{name} is {len(lines)} lines"

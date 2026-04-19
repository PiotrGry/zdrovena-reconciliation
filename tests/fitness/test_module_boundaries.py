"""
Fitness Functions — Module Boundary Constraints
================================================
Egzekwują architektoniczne reguły zależności między modułami zdrovena.
Uruchamiane w CI jako osobny job.

Dozwolone zależności (acyclic):
    common        → (nic)
    audit         → common
    month_closing → common, audit
    api           → common, month_closing, audit
    cli.py        → wszystkie (entry point)

Zabronione:
    audit         → month_closing  (cykl / naruszenie granic)
    audit         → api
    common        → cokolwiek z zdrovena
    month_closing → api            (odwrócona zależność — api zna month_closing, nie odwrotnie)
                                   wyjątek: lazy import w commands/ jest zaakceptowany świadomie
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).parent.parent.parent / "zdrovena"

FORBIDDEN: list[tuple[str, str, str]] = [
    # (plik glob pattern, zakazany import, powód)
    ("audit/**/*.py",         "zdrovena.month_closing", "audit nie może importować month_closing — cykl"),
    ("audit/**/*.py",         "zdrovena.api",           "audit nie może importować api"),
    ("common/**/*.py",        "zdrovena.audit",         "common jest liściem — brak zależności w górę"),
    ("common/**/*.py",        "zdrovena.month_closing", "common jest liściem — brak zależności w górę"),
    ("common/**/*.py",        "zdrovena.api",           "common jest liściem — brak zależności w górę"),
]

# Świadome wyjątki — lazy imports zaakceptowane architektonicznie
EXCEPTIONS: set[tuple[str, str]] = {
    # month_closing/commands/close_cmd.py używa lazy import zdrovena.api.client
    # w funkcji _run_api() — celowy design z Fazy D (CLI routing do API)
    ("month_closing/commands/close_cmd.py", "zdrovena.api"),
}


def _get_imports(filepath: pathlib.Path) -> list[str]:
    """Zwraca listę modułów zdrovena importowanych w pliku."""
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("zdrovena."):
                parts = node.module.split(".")
                # Normalizuj do zdrovena.X (top-level module)
                target = ".".join(parts[:2])
                imports.append(target)
    return imports


def _relative(filepath: pathlib.Path) -> str:
    return str(filepath.relative_to(ROOT))


class TestModuleBoundaries:
    def test_no_forbidden_imports(self) -> None:
        """Żaden moduł nie narusza reguł zależności."""
        violations: list[str] = []

        for pattern, forbidden_import, reason in FORBIDDEN:
            for filepath in ROOT.glob(pattern):
                if not filepath.is_file():
                    continue
                rel = _relative(filepath)
                if (rel, forbidden_import) in EXCEPTIONS:
                    continue
                imports = _get_imports(filepath)
                if forbidden_import in imports:
                    violations.append(
                        f"  {rel} → {forbidden_import}\n    Powód: {reason}"
                    )

        assert not violations, (
            f"\n\nNaruszenia granic modułów ({len(violations)}):\n"
            + "\n".join(violations)
            + "\n\nNapraw import lub dodaj świadomy wyjątek do EXCEPTIONS w tym pliku."
        )

    def test_common_has_no_zdrovena_imports(self) -> None:
        """common/ jest liściem — nie importuje innych modułów zdrovena."""
        violations: list[str] = []
        for filepath in ROOT.glob("common/**/*.py"):
            imports = _get_imports(filepath)
            bad = [i for i in imports if not i.startswith("zdrovena.common")]
            if bad:
                violations.append(f"  {_relative(filepath)} → {bad}")

        assert not violations, (
            f"\n\ncommon/ nie może importować innych modułów zdrovena:\n"
            + "\n".join(violations)
        )

    def test_audit_does_not_import_month_closing(self) -> None:
        """audit/ nie importuje month_closing/ — zapobiega cyklom."""
        violations: list[str] = []
        for filepath in ROOT.glob("audit/**/*.py"):
            imports = _get_imports(filepath)
            if "zdrovena.month_closing" in imports:
                violations.append(f"  {_relative(filepath)}")

        assert not violations, (
            "\n\naudit/ importuje month_closing/ — to tworzy cykl:\n"
            + "\n".join(violations)
            + "\n\nPrzenieś współdzielone stałe do zdrovena.common"
        )

    def test_documented_exceptions_still_needed(self) -> None:
        """Sprawdza czy wszystkie wyjątki w EXCEPTIONS nadal istnieją w kodzie.

        Jeśli ten test padnie — wyjątek można usunąć (kod się zmienił).
        """
        for rel_path, forbidden_import in EXCEPTIONS:
            filepath = ROOT / rel_path
            assert filepath.exists(), (
                f"Wyjątek w EXCEPTIONS wskazuje na nieistniejący plik: {rel_path}\n"
                f"Usuń go z EXCEPTIONS."
            )
            imports = _get_imports(filepath)
            assert forbidden_import in imports, (
                f"Wyjątek w EXCEPTIONS nie jest już potrzebny:\n"
                f"  {rel_path} nie importuje już {forbidden_import}\n"
                f"Usuń go z EXCEPTIONS."
            )

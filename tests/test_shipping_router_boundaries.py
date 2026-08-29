"""Boundaries of the split shipping routers (issue #313).

`webhooks.py` grew to 2000 lines because nothing objected while it did. These
rules object: they fail on the commit that widens a router, not months later
when someone tries to find where an endpoint lives.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG = REPO_ROOT / "zdrovena" / "api" / "routers" / "shipping"

#: Routers may compose; they may not contain the business rules they compose.
#: These packages own those rules.
APPLICATION_PACKAGES = ("zdrovena.shipping.application", "zdrovena.shipping.domain")


def _router_modules() -> list[Path]:
    return sorted(p for p in PKG.glob("*.py") if p.name not in ("__init__.py", "deps.py"))


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheSplitHappened:
    def test_every_responsibility_has_its_own_module(self):
        names = {p.stem for p in PKG.glob("*.py")}

        assert {
            "ingestion",
            "drafts",
            "execution",
            "fulfillment",
            "labels",
            "invoices",
            "dlq",
            "test_support",
        } <= names

    def test_the_old_module_holds_no_logic(self):
        """It stays only so existing imports of `webhooks.router` keep working."""
        legacy = (REPO_ROOT / "zdrovena" / "api" / "routers" / "webhooks.py").read_text()

        assert "@router." not in legacy
        assert len(legacy.splitlines()) < 30

    @pytest.mark.parametrize("path", _router_modules(), ids=lambda p: p.stem)
    def test_no_router_grows_back_to_the_old_size(self, path):
        """2000 lines is how the previous one got there — one addition at a time."""
        lines = len(_source(path).splitlines())

        assert lines < 700, f"{path.name} is {lines} lines"


class TestRoutersAreNotApplicationServices:
    @pytest.mark.parametrize("path", _router_modules(), ids=lambda p: p.stem)
    def test_a_router_does_not_reimplement_the_application_layer(self, path):
        """Routers call into shipping.application; they must not become it."""
        source = _source(path)
        if not any(pkg in source for pkg in APPLICATION_PACKAGES):
            return
        tree = ast.parse(source)
        functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        oversized = [
            f"{n.name} ({(n.end_lineno or 0) - n.lineno} lines)"
            for n in functions
            if (n.end_lineno or 0) - n.lineno > 140
        ]

        assert oversized == [], f"{path.name}: {oversized}"


class TestDependencyDirection:
    @pytest.mark.parametrize("path", _router_modules(), ids=lambda p: p.stem)
    def test_routers_do_not_import_each_other(self, path):
        """Sideways imports are how one module quietly becomes the hub again.

        Shared composition belongs in `deps`, which is the single exception.
        """
        tree = ast.parse(_source(path))
        siblings = {p.stem for p in _router_modules()} - {path.stem}
        offenders = []
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = ",".join(a.name for a in node.names)
            for sibling in siblings:
                if f"routers.shipping.{sibling}" in module:
                    offenders.append(sibling)
                if module.endswith("routers.shipping"):
                    offenders.extend(
                        a.name for a in getattr(node, "names", []) if a.name in siblings
                    )

        assert offenders == [], f"{path.name} imports sibling routers: {sorted(set(offenders))}"

    def test_shared_composition_lives_in_one_module(self):
        """`deps` exists so secrets and client construction are not duplicated."""
        deps = (PKG / "deps.py").read_text()

        assert "get_secret" in deps

    def test_deps_does_not_import_the_routers(self):
        """It is depended upon; depending back would make the cycle real.

        Checked against real imports rather than the text: the module's own
        logger name contains that dotted path and is not a dependency.
        """
        tree = ast.parse((PKG / "deps.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)

        offenders = sorted(m for m in imported if "routers.shipping" in m)

        assert offenders == [], f"deps imports routers: {offenders}"


class TestTestSupportFailsClosed:
    def test_the_test_endpoints_live_in_their_own_module(self):
        assert (PKG / "test_support.py").exists()

    def test_every_test_endpoint_is_gated(self):
        """Ungated, these would let anyone reset shipping state in production."""
        tree = ast.parse(_source(PKG / "test_support.py"))
        handlers = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and any("router." in ast.unparse(d) for d in n.decorator_list)
        ]

        assert handlers, "no test-support endpoints found"
        for handler in handlers:
            assert "_require_test_support()" in ast.unparse(handler), handler.name

    def test_the_gate_refuses_production(self, monkeypatch):
        from zdrovena.api.routers.shipping import deps

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("ENABLE_TEST_SUPPORT", raising=False)

        assert deps._test_support_enabled() is False

    def test_the_gate_refuses_an_ambiguous_environment(self, monkeypatch):
        """Unknown is not permission."""
        from zdrovena.api.routers.shipping import deps

        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("ENABLE_TEST_SUPPORT", raising=False)

        assert deps._test_support_enabled() is False

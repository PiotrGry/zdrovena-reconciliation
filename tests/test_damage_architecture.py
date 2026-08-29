"""Boundaries of the damage module (issue #317).

Architecture erodes one convenient edit at a time: a status string written
straight into a handler because the workflow call was two lines away. Nothing
fails when that happens — the endpoint still works — so only a test notices.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APPLICATION_DIR = REPO_ROOT / "zdrovena" / "damage" / "application"
ROUTER = REPO_ROOT / "zdrovena" / "api" / "routers" / "damage.py"

#: Packages the application layer must not reach for. It may know what it needs
#: from the outside (ports.py), never who provides it.
FORBIDDEN_IN_APPLICATION = ("fastapi", "starlette", "azure", "zdrovena.api")


def _imported_modules(path: Path) -> set[str]:
    """Module-level and function-level imports alike — a lazy import inside a
    function is still a dependency, just a harder one to see."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _application_files() -> list[Path]:
    return sorted(p for p in APPLICATION_DIR.rglob("*.py"))


class TestApplicationLayerIsIndependent:
    def test_the_layer_exists(self):
        assert _application_files(), "no application layer found"

    @pytest.mark.parametrize("path", _application_files(), ids=lambda p: p.name)
    def test_it_imports_no_web_framework_or_provider_client(self, path):
        for module in _imported_modules(path):
            root = module.split(".")[0]
            assert root not in ("fastapi", "starlette", "azure"), f"{path.name} imports {module}"
            assert not module.startswith("zdrovena.api"), f"{path.name} imports {module}"

    def test_it_does_not_raise_http_exceptions(self):
        """Refusals leave as domain errors; the status code is the router's call."""
        for path in _application_files():
            assert "HTTPException" not in path.read_text(encoding="utf-8"), path.name


class TestRouterDoesNotOwnTheWorkflow:
    def _router_source(self) -> str:
        return ROUTER.read_text(encoding="utf-8")

    def test_the_router_does_not_write_workflow_state_transitions(self):
        """Every one of these used to be assigned inline in a handler."""
        source = self._router_source()
        transitions = (
            "replacement_prepared",
            "replacement_created",
            "replacement_pending",
            "customer_notified",
        )
        leaked = [t for t in transitions if f'"{t}"' in source]

        assert leaked == [], f"router assigns workflow states: {leaked}"

    def test_the_router_does_not_build_the_replacement_draft(self):
        source = self._router_source()

        assert "is_replacement" not in source
        assert "deepcopy" not in source

    def test_the_router_does_not_compose_the_customer_email(self):
        source = self._router_source()

        assert "Dzień dobry" not in source
        assert "Zespół HUMIO" not in source

    def test_workflow_handlers_only_delegate(self):
        """Each handler maps a request, calls the workflow, maps errors back.

        A handler growing past this is where the rules start living in two
        places again.
        """
        tree = ast.parse(self._router_source())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            is_route = any("router." in ast.unparse(d) for d in node.decorator_list)
            if not is_route:
                continue
            calls_workflow = "_workflow(" in ast.unparse(node)
            if not calls_workflow:
                continue
            body_lines = (node.end_lineno or 0) - node.lineno
            if body_lines > 30:
                offenders.append(f"{node.name} ({body_lines} lines)")

        assert offenders == [], f"handlers doing more than delegating: {offenders}"


class TestErrorMappingIsExhaustive:
    def test_every_domain_error_has_a_status_code(self):
        """An unmapped error would fall through to a generic 409 and quietly
        report the wrong thing."""
        from zdrovena.damage.application import errors as error_module

        router_source = ROUTER.read_text(encoding="utf-8")
        table = router_source.split("_ERROR_STATUS")[1].split("}")[0]

        declared = {
            name
            for name, obj in vars(error_module).items()
            if isinstance(obj, type)
            and issubclass(obj, error_module.DamageWorkflowError)
            and obj is not error_module.DamageWorkflowError
        }
        unmapped = sorted(name for name in declared if not re.search(rf"\b{name}\b", table))

        assert unmapped == [], f"domain errors with no status code: {unmapped}"

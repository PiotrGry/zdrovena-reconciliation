"""Log noise control (issue #213).

The Azure Monitor distro attaches its exporting handler to the ROOT logger --
`logger_name` defaults to "" -- so every INFO record from every library in the
process is shipped to Log Analytics. Pinning a hand-written list of logger
names can only ever cover the names somebody thought of; the tests below pin
the behaviour that does not depend on that list.
"""

from __future__ import annotations

import logging

import pytest

from zdrovena.common.logging_setup import (
    NOISY_SDK_LOGGERS,
    export_severity_filter,
    install_export_filter,
    quiet_sdk_loggers,
)


def _record(name: str, level: int) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, "msg", None, None)


# ── The export filter ────────────────────────────────────────────────────────


def test_third_party_info_is_not_exported():
    keep = export_severity_filter(logging.WARNING)

    assert keep.filter(_record("azure.identity", logging.INFO)) is False
    assert keep.filter(_record("opentelemetry.sdk.metrics", logging.INFO)) is False
    assert keep.filter(_record("urllib3.connectionpool", logging.INFO)) is False


def test_third_party_warnings_and_errors_are_still_exported():
    """A dependency failure must stay visible in Log Analytics."""
    keep = export_severity_filter(logging.WARNING)

    assert keep.filter(_record("azure.identity", logging.WARNING)) is True
    assert keep.filter(_record("azure.data.tables", logging.ERROR)) is True
    assert keep.filter(_record("azure.core", logging.CRITICAL)) is True


def test_application_logs_are_exported_at_every_level():
    """zdrovena.events is the whole point of the pipeline -- never filter it."""
    keep = export_severity_filter(logging.WARNING)

    assert keep.filter(_record("zdrovena.events", logging.INFO)) is True
    assert keep.filter(_record("zdrovena.api.routers.webhooks", logging.INFO)) is True
    assert keep.filter(_record("zdrovena.api.main", logging.DEBUG)) is True


def test_a_lower_threshold_lets_third_party_info_through():
    """Configurable per environment: drop the threshold to debug an incident."""
    keep = export_severity_filter(logging.DEBUG)

    assert keep.filter(_record("azure.identity", logging.INFO)) is True


# ── Installing the filter on the exporting handler ───────────────────────────


class _FakeExportHandler(logging.Handler):
    """Stands in for opentelemetry.sdk._logs.LoggingHandler."""


def test_install_targets_the_handler_that_exports(monkeypatch):
    root = logging.getLogger()
    handler = _FakeExportHandler()
    root.addHandler(handler)
    monkeypatch.setattr(
        "zdrovena.common.logging_setup._export_handler_types",
        lambda: (_FakeExportHandler,),
    )
    try:
        assert install_export_filter(threshold=logging.WARNING) == 1
        assert any(getattr(f, "is_export_filter", False) for f in handler.filters)
    finally:
        root.removeHandler(handler)


def test_install_is_idempotent(monkeypatch):
    root = logging.getLogger()
    handler = _FakeExportHandler()
    root.addHandler(handler)
    monkeypatch.setattr(
        "zdrovena.common.logging_setup._export_handler_types",
        lambda: (_FakeExportHandler,),
    )
    try:
        install_export_filter(threshold=logging.WARNING)
        install_export_filter(threshold=logging.WARNING)
        installed = [f for f in handler.filters if getattr(f, "is_export_filter", False)]
        assert len(installed) == 1
    finally:
        root.removeHandler(handler)


def test_install_leaves_console_handlers_alone(monkeypatch):
    """Reducing what we pay to ingest must not reduce what a developer sees."""
    root = logging.getLogger()
    console = logging.StreamHandler()
    root.addHandler(console)
    monkeypatch.setattr(
        "zdrovena.common.logging_setup._export_handler_types",
        lambda: (_FakeExportHandler,),
    )
    try:
        install_export_filter(threshold=logging.WARNING)
        assert console.filters == []
    finally:
        root.removeHandler(console)


def test_install_without_an_exporter_is_a_no_op(monkeypatch):
    monkeypatch.setattr(
        "zdrovena.common.logging_setup._export_handler_types",
        lambda: (_FakeExportHandler,),
    )
    assert install_export_filter(threshold=logging.WARNING) == 0


def test_threshold_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL_AZURE_EXPORT", "ERROR")
    root = logging.getLogger()
    handler = _FakeExportHandler()
    root.addHandler(handler)
    monkeypatch.setattr(
        "zdrovena.common.logging_setup._export_handler_types",
        lambda: (_FakeExportHandler,),
    )
    try:
        install_export_filter()
        installed = next(f for f in handler.filters if getattr(f, "is_export_filter", False))
        assert installed.filter(_record("azure.identity", logging.WARNING)) is False
        assert installed.filter(_record("azure.identity", logging.ERROR)) is True
    finally:
        root.removeHandler(handler)


def test_an_unreadable_threshold_falls_back_to_warning(monkeypatch):
    """A typo in configuration must not silently disable the whole pipeline."""
    monkeypatch.setenv("LOG_LEVEL_AZURE_EXPORT", "LOUD")
    keep = export_severity_filter(None)

    assert keep.filter(_record("azure.identity", logging.INFO)) is False
    assert keep.filter(_record("azure.identity", logging.WARNING)) is True


# ── The console-side logger pinning ──────────────────────────────────────────


def test_quiet_sdk_loggers_pins_every_listed_logger(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL_AZURE", raising=False)
    quiet_sdk_loggers()

    for name in NOISY_SDK_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name


def test_the_exporter_is_covered_by_its_parent():
    """azure.monitor.opentelemetry.exporter inherits the pinned parent level."""
    quiet_sdk_loggers("WARNING")
    child = logging.getLogger("azure.monitor.opentelemetry.exporter.export._base")

    assert child.getEffectiveLevel() == logging.WARNING


def test_quiet_sdk_loggers_respects_the_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL_AZURE", "DEBUG")
    quiet_sdk_loggers()

    assert logging.getLogger("azure.identity").level == logging.DEBUG


@pytest.fixture(autouse=True)
def _restore_sdk_logger_levels():
    names = list(NOISY_SDK_LOGGERS)
    before = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in before.items():
        logging.getLogger(name).setLevel(level)


# ── Policy ───────────────────────────────────────────────────────────────────


def test_the_noisy_logger_list_lives_in_one_place():
    """It used to be copy-pasted into main.py and allegro_poll_cmd.py.

    Two copies drift, and the one nobody remembers is the one that keeps
    shipping noise.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "zdrovena"
    marker = "azure.core.pipeline.policies.http_logging_policy"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "logging_setup.py" and marker in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"noisy-logger list duplicated in: {offenders}"

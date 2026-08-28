"""Shared logging configuration, and control over what reaches Log Analytics.

Two separate concerns live here, and they are deliberately not the same knob:

* ``quiet_sdk_loggers`` pins chatty Azure SDK loggers so a developer's console
  is readable. It affects every handler, including stdout.
* ``install_export_filter`` limits what the Azure Monitor exporter *ships*.
  The distro attaches its handler to the ROOT logger -- ``logger_name``
  defaults to ``""`` -- so without a filter every INFO record produced by every
  library in the process is ingested and billed. Over 99% of AppTraces was
  third-party chatter (issue #213).

The filter is severity-based rather than name-based on purpose. A list of noisy
logger names only ever covers the names somebody thought of; the next noisy
dependency arrives unlisted. Third-party records need WARNING or worse to be
exported, application records are exported at whatever level they were emitted,
so a dependency failure stays visible while its success chatter does not.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("zdrovena.common.logging_setup")

# Pinned so local runs stay readable. Children inherit the level, so
# "azure.monitor.opentelemetry" also covers ".exporter.export._base".
NOISY_SDK_LOGGERS: tuple[str, ...] = (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.storage",
    "azure.data.tables",
    "azure.monitor.opentelemetry",
)

# Records from these logger trees are ours and are always exported.
APPLICATION_LOGGER_PREFIXES: tuple[str, ...] = ("zdrovena",)

_DEFAULT_EXPORT_THRESHOLD = logging.WARNING


class _ExportSeverityFilter(logging.Filter):
    """Export our own records unconditionally, third-party ones by severity."""

    is_export_filter = True

    def __init__(self, threshold: int) -> None:
        super().__init__()
        self.threshold = threshold

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(APPLICATION_LOGGER_PREFIXES):
            return True
        return record.levelno >= self.threshold


def _resolve_threshold(threshold: int | None) -> int:
    if threshold is not None:
        return threshold
    configured = os.environ.get("LOG_LEVEL_AZURE_EXPORT", "").strip().upper()
    if not configured:
        return _DEFAULT_EXPORT_THRESHOLD
    level = logging.getLevelName(configured)
    if not isinstance(level, int):
        # A typo must not silently disable the reduction or the pipeline.
        logger.warning(
            "LOG_LEVEL_AZURE_EXPORT=%s is not a log level — falling back to WARNING.",
            configured,
        )
        return _DEFAULT_EXPORT_THRESHOLD
    return level


def export_severity_filter(threshold: int | None = None) -> _ExportSeverityFilter:
    """Build the filter used to decide what the exporter ships."""

    return _ExportSeverityFilter(_resolve_threshold(threshold))


def _export_handler_types() -> tuple[type, ...]:
    """The handler classes that ship records off the machine.

    Imported lazily: OpenTelemetry is an optional dependency, and a process
    without it has nothing to filter.
    """

    try:
        from opentelemetry.sdk._logs import LoggingHandler
    except Exception:  # pragma: no cover - depends on optional extras
        return ()
    return (LoggingHandler,)


def install_export_filter(threshold: int | None = None) -> int:
    """Attach the severity filter to every exporting handler on the root logger.

    Returns the number of handlers filtered. Idempotent — calling it twice does
    not stack two filters. Console handlers are left untouched: reducing what we
    pay to ingest must not reduce what a developer sees.
    """

    export_types = _export_handler_types()
    if not export_types:
        return 0

    installed = 0
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, export_types):
            continue
        if any(getattr(existing, "is_export_filter", False) for existing in handler.filters):
            installed += 1
            continue
        handler.addFilter(export_severity_filter(threshold))
        installed += 1
    return installed


def quiet_sdk_loggers(level: str | None = None) -> None:
    """Pin the chatty Azure SDK loggers. ``LOG_LEVEL_AZURE`` overrides."""

    resolved = (level or os.environ.get("LOG_LEVEL_AZURE", "WARNING")).upper()
    for name in NOISY_SDK_LOGGERS:
        logging.getLogger(name).setLevel(resolved)


def configure_process_logging(
    *,
    log_format: str,
    filters: tuple[logging.Filter, ...] = (),
) -> None:
    """Set up stdout logging for a process entrypoint.

    ``filters`` are attached to the root handlers, which is the only place that
    can guarantee every record carries an attribute the format string needs.
    """

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format=log_format,
        force=True,
    )
    for handler in logging.getLogger().handlers:
        for log_filter in filters:
            handler.addFilter(log_filter)
    quiet_sdk_loggers()

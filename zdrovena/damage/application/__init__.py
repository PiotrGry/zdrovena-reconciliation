"""Application layer for the damaged-shipment workflow.

Imports no web framework and no concrete provider client. Everything the
workflow needs from the outside arrives through the protocols in ``ports``,
so the whole confirm → replacement → shipment → email → close chain is
testable without starting HTTP (issue #317).
"""

from zdrovena.damage.application.errors import (
    CaseNotFound,
    CorrelationFailed,
    DamageWorkflowError,
    InvalidTransition,
    MailNotConfigured,
    MailSenderNotAllowed,
    SendBlocked,
)
from zdrovena.damage.application.workflow import DamageWorkflow

__all__ = [
    "CaseNotFound",
    "CorrelationFailed",
    "DamageWorkflow",
    "DamageWorkflowError",
    "InvalidTransition",
    "MailNotConfigured",
    "MailSenderNotAllowed",
    "SendBlocked",
]

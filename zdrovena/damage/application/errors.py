"""Domain errors. The router maps these to HTTP; nothing here knows about it.

Each carries the operator-facing message the API used to build inline, so the
existing responses stay byte-for-byte identical after the move.
"""

from __future__ import annotations


class DamageWorkflowError(RuntimeError):
    """Base for every refusal the workflow can express."""


class CaseNotFound(DamageWorkflowError):
    """No damage case with that id."""


class InvalidTransition(DamageWorkflowError):
    """The case is not in a state where this step is allowed."""


class CorrelationFailed(DamageWorkflowError):
    """The tracking number could not be matched to a shipping draft."""


class MailNotConfigured(DamageWorkflowError):
    """The mail provider is not usable at all."""


class MailSenderNotAllowed(DamageWorkflowError):
    """The configured From address is not active with the provider."""


class SendBlocked(DamageWorkflowError):
    """A previous send attempt is unresolved, so sending again could duplicate."""


class MailDeliveryFailed(DamageWorkflowError):
    """The provider refused the message."""

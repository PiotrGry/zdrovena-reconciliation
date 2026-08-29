"""What the workflow needs from the outside world, as protocols.

Defined here rather than imported so the application layer never reaches for a
concrete Azure, SMTP or FastAPI type. The adapters live in the API layer.
"""

from __future__ import annotations

from typing import Any, Protocol


class DamageCaseStore(Protocol):
    def get_case(self, case_id: str) -> dict[str, Any] | None: ...

    def update_case(self, case_id: str, fields: dict[str, Any]) -> Any: ...

    def try_claim_email(self, case_id: str, attempt: dict[str, Any] | None = None) -> bool: ...


class DraftStore(Protocol):
    def get_draft(self, draft_id: str) -> dict[str, Any] | None: ...

    def upsert_draft(self, record: dict[str, Any]) -> None: ...

    def update_draft(self, draft_id: str, fields: dict[str, Any]) -> Any: ...

    def list_drafts(self, limit: int = ...) -> list[dict[str, Any]]: ...


class ShipmentExecutor(Protocol):
    """Creating and confirming a courier shipment.

    Provider and HTTP concerns stay on the adapter's side of this line: the
    workflow calls these and lets whatever they raise travel outward, so the
    API layer keeps owning the mapping it already owns.
    """

    def execute(self, draft_id: str) -> dict[str, Any]: ...

    def confirm(self, draft_id: str) -> dict[str, Any]: ...


class MailGateway(Protocol):
    def sender_addresses(self) -> set[str]: ...

    def send(self, *, to: str, subject: str, body: str) -> dict[str, Any] | None: ...

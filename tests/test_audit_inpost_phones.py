"""Tests for scripts/audit-inpost-phones.py.

The script exists so the operator finds affected drafts before InPost starts
enforcing on 2026-09-08, rather than one failed shipment at a time afterwards.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location(
    "audit_inpost_phones",
    Path(__file__).resolve().parent.parent / "scripts" / "audit-inpost-phones.py",
)
assert _SPEC and _SPEC.loader
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "id": "d1",
        "shopify_order_number": "1700",
        "courier": "inpost",
        "status": "pending",
        "receiver": {"phone": "+48600100200"},
    }
    draft.update(overrides)
    return draft


class TestNeedsAttention:
    def test_flags_an_inpost_draft_without_a_usable_phone(self) -> None:
        assert audit.needs_attention(_draft(receiver={"phone": None})) is True
        assert audit.needs_attention(_draft(receiver={"phone": ""})) is True
        assert audit.needs_attention(_draft(receiver={"phone": "12345"})) is True
        assert audit.needs_attention(_draft(receiver={})) is True
        assert audit.needs_attention(_draft(receiver=None)) is True

    def test_ignores_every_format_inpost_accepts(self) -> None:
        for raw in ("+48 000 000 000", "48 000 000 000", "000 000 000", "000000000"):
            assert audit.needs_attention(_draft(receiver={"phone": raw})) is False

    def test_ignores_other_carriers(self) -> None:
        assert audit.needs_attention(_draft(courier="apaczka", receiver={"phone": None})) is False
        assert (
            audit.needs_attention(_draft(courier="allegro_delivery", receiver={"phone": None}))
            is False
        )

    def test_ignores_terminal_statuses(self) -> None:
        # A shipment already created or cancelled will never be posted again.
        for status in ("created", "cancelled"):
            assert audit.needs_attention(_draft(status=status, receiver={"phone": None})) is False

    def test_still_flags_a_draft_waiting_for_review(self) -> None:
        for status in ("needs_review", "pending", "error"):
            assert audit.needs_attention(_draft(status=status, receiver={"phone": None})) is True

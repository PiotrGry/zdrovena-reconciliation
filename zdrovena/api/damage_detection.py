"""Detection and correlation service for damaged-parcel notifications.

Detection is intentionally read-only with respect to courier systems. It may
create a local case in ``needs_review`` but never creates a replacement parcel
or contacts a customer. Those are separate operator actions in the API/UI.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from zdrovena.common.apaczka import ApaczkaClient
from zdrovena.common.config import (
    KEYCHAIN_SERVICE_ZOHO_CLIENT_ID,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET,
    KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN,
)
from zdrovena.common.damage_store import DamageStore
from zdrovena.common.secrets import get_secret
from zdrovena.month_closing.zoho_mail import ZohoMailClient

logger = logging.getLogger("zdrovena.api.damage_detection")

_RECENT_DISCOVERY_COUNT = 20
_ROUND_ROBIN_DISCOVERY_COUNT = 30

_DAMAGE_WORDS = re.compile(
    r"(?:uszkodzon\w*|uszkodzeni\w*|zniszczon\w*|parcel\s+(?:has\s+been\s+)?damaged|"
    r"damaged\s+parcel|shipment\s+(?:has\s+been\s+)?damaged)",
    re.IGNORECASE,
)
_INPOST_DAMAGE_SENDER = re.compile(r"^uszkodz(?:on|eni)[^@]*@inpost\.pl$", re.IGNORECASE)
_INPOST_CENTRAL_SENDER = "dyspozycje_biznes@inpost.pl"
_INPOST_TRACKING = re.compile(r"(?<!\d)(\d{24})(?!\d)")
_CASE_NAMESPACE = uuid.UUID("bba9bba0-6699-4dc0-884d-f089ab85e590")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_damage_description(text: str) -> bool:
    return bool(_DAMAGE_WORDS.search(text or ""))


def is_allowed_inpost_sender(address: str) -> bool:
    normalized = (address or "").strip().lower()
    return normalized == _INPOST_CENTRAL_SENDER or bool(_INPOST_DAMAGE_SENDER.fullmatch(normalized))


def extract_inpost_tracking(subject: str, content: str) -> str | None:
    """Extract a 24-digit InPost number, preferring the subject."""
    for text in (subject, content):
        match = _INPOST_TRACKING.search(text or "")
        if match:
            return match.group(1)
    return None


def _case_id(tracking_number: str) -> str:
    return str(uuid.uuid5(_CASE_NAMESPACE, tracking_number.strip().upper()))


def _draft_for_tracking(drafts: list[dict[str, Any]], tracking: str) -> dict[str, Any] | None:
    normalized = tracking.strip().upper()
    for draft in drafts:
        if str(draft.get("tracking_number") or "").strip().upper() == normalized:
            return draft
    return None


def _case_context(draft: dict[str, Any] | None) -> dict[str, Any]:
    if not draft:
        return {
            "shipping_draft_id": None,
            "order_number": None,
            "external_order_id": None,
            "customer_name": None,
            "customer_email": None,
            "courier": None,
        }
    receiver = draft.get("receiver") or {}
    return {
        "shipping_draft_id": draft.get("id"),
        "order_number": draft.get("shopify_order_number"),
        "external_order_id": draft.get("external_order_id"),
        "customer_name": draft.get("customer_name"),
        "customer_email": receiver.get("email") if isinstance(receiver, dict) else None,
        "courier": draft.get("courier"),
    }


def _upsert_detected_case(
    damage_store: DamageStore,
    *,
    tracking_number: str,
    source: str,
    classification: str,
    detected_at: str,
    fingerprint: str,
    evidence: dict[str, Any],
    draft: dict[str, Any] | None,
    provider_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    case_id = _case_id(tracking_number)
    existing = damage_store.get_case(case_id)
    now = _now()
    context = {**_case_context(draft), **(provider_context or {})}
    if existing:
        fingerprints = list(existing.get("event_fingerprints") or [])
        is_new_evidence = fingerprint not in fingerprints
        if is_new_evidence:
            fingerprints.append(fingerprint)
        evidence_items = list(existing.get("evidence") or [])
        if is_new_evidence:
            evidence_items.append(evidence)
        sources = list(existing.get("sources") or [])
        if source not in sources:
            sources.append(source)
        fields: dict[str, Any] = {
            "updated_at": now,
            "sources": sources,
            "event_fingerprint": fingerprint,
            "event_fingerprints": fingerprints,
            "evidence": evidence_items[-20:],
        }
        for key, value in context.items():
            if value and not existing.get(key):
                fields[key] = value
        if classification == "damage" and existing.get("classification") != "damage":
            fields["classification"] = "damage"
            fields["confidence"] = "high"
        damage_store.update_case(case_id, fields)
        return damage_store.get_case(case_id) or {**existing, **fields}, False

    record: dict[str, Any] = {
        "id": case_id,
        "created_at": now,
        "updated_at": now,
        "detected_at": detected_at or now,
        "status": "needs_review",
        "classification": classification,
        "confidence": "high" if classification == "damage" else "medium",
        "tracking_number": tracking_number,
        "sources": [source],
        "event_fingerprint": fingerprint,
        "event_fingerprints": [fingerprint],
        "evidence": [evidence],
        "replacement_draft_id": None,
        "replacement_tracking_number": None,
        "email_draft": None,
        "email_sent_at": None,
        **context,
    }
    damage_store.upsert_case(record)
    return record, True


def _shipment_tracking(draft: dict[str, Any]) -> tuple[str, str] | None:
    tracking = str(draft.get("tracking_number") or "").strip()
    if not tracking:
        return None
    carrier = str(draft.get("tracking_carrier_id") or "").strip()
    if not carrier:
        courier = draft.get("courier")
        carrier = (
            "ALLEGRO"
            if courier == "allegro_delivery"
            else "INPOST"
            if courier == "inpost"
            else "OTHER"
        )
    return carrier, tracking


def scan_allegro_damage_cases(
    *,
    client: Any,
    shipping_store: Any,
    damage_store: DamageStore,
) -> dict[str, int]:
    """Scan complete Allegro tracking histories and create manual-review cases."""
    stats = {
        "drafts": 0,
        "shipments": 0,
        "issues": 0,
        "created": 0,
        "deferred": 0,
        "errors": 0,
    }
    drafts = [
        draft
        for draft in shipping_store.list_drafts(limit=500)
        if draft.get("source") == "allegro" and draft.get("status") != "cancelled"
    ]
    stats["drafts"] = len(drafts)
    by_tracking: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[str]] = defaultdict(list)

    missing_tracking: list[dict[str, Any]] = []
    for draft in drafts:
        known = _shipment_tracking(draft)
        if known:
            carrier, tracking = known
            grouped[carrier].append(tracking)
            by_tracking[tracking.upper()] = draft
            continue
        missing_tracking.append(draft)

    # Keep the 5-minute job bounded. Always inspect the newest missing drafts,
    # then rotate through older ones so a large historical backlog cannot cause
    # hundreds of sequential Allegro calls or starve forever.
    recent = missing_tracking[:_RECENT_DISCOVERY_COUNT]
    older = missing_tracking[_RECENT_DISCOVERY_COUNT:]
    selected = list(recent)
    if older:
        cursor = int(damage_store.get_state("allegro_discovery_cursor", 0)) % len(older)
        take = min(_ROUND_ROBIN_DISCOVERY_COUNT, len(older))
        selected.extend(older[(cursor + index) % len(older)] for index in range(take))
        damage_store.set_state("allegro_discovery_cursor", (cursor + take) % len(older))
    stats["deferred"] = max(0, len(missing_tracking) - len(selected))

    for draft in selected:
        order_id = str(draft.get("external_order_id") or "")
        if not order_id:
            continue
        try:
            shipments = client.get_shipments(order_id)
        except Exception:
            logger.exception("Could not discover Allegro shipments for order %s", order_id)
            stats["errors"] += 1
            continue
        for shipment in shipments:
            tracking = str(shipment.get("waybill") or "").strip()
            carrier = str(shipment.get("carrierId") or "").strip()
            if not tracking or not carrier:
                continue
            grouped[carrier].append(tracking)
            by_tracking[tracking.upper()] = draft
            stats["shipments"] += 1
            try:
                shipping_store.update_draft(
                    str(draft["id"]),
                    {
                        "tracking_number": tracking,
                        "tracking_carrier_id": carrier,
                        "allegro_order_shipment_id": shipment.get("id"),
                    },
                )
                draft["tracking_number"] = tracking
                draft["tracking_carrier_id"] = carrier
            except Exception:
                logger.exception("Could not persist discovered tracking %s", tracking)
                stats["errors"] += 1

    for carrier, all_waybills in grouped.items():
        waybills = list(dict.fromkeys(all_waybills))
        for offset in range(0, len(waybills), 20):
            batch = waybills[offset : offset + 20]
            try:
                response = client.get_tracking_history(carrier, batch)
            except Exception:
                logger.exception("Could not read Allegro tracking for %s", carrier)
                stats["errors"] += 1
                continue
            for parcel in response.get("waybills") or []:
                tracking = str(parcel.get("waybill") or "").strip()
                details = parcel.get("trackingDetails") or {}
                statuses = details.get("statuses") or []
                for event in statuses:
                    code = str(event.get("code") or "").upper()
                    description = str(event.get("description") or "")
                    damaged = is_damage_description(description)
                    if code != "ISSUE" and not damaged:
                        continue
                    stats["issues"] += 1
                    occurred_at = str(event.get("occurredAt") or details.get("updatedAt") or _now())
                    fingerprint = f"allegro:{tracking}:{occurred_at}:{code}"
                    _case, created = _upsert_detected_case(
                        damage_store,
                        tracking_number=tracking,
                        source="allegro_tracking",
                        classification="damage" if damaged else "carrier_issue",
                        detected_at=occurred_at,
                        fingerprint=fingerprint,
                        evidence={
                            "source": "allegro_tracking",
                            "carrier_id": carrier,
                            "code": code,
                            "description": description,
                            "occurred_at": occurred_at,
                        },
                        draft=by_tracking.get(tracking.upper()),
                    )
                    stats["created"] += int(created)
    return stats


def scan_zoho_damage_cases(
    *,
    client: ZohoMailClient,
    shipping_store: Any,
    damage_store: DamageStore,
    apaczka_client: Any | None = None,
) -> dict[str, int]:
    """Read-only Zoho scan for trusted InPost damage notifications."""
    stats = {
        "messages": 0,
        "matched": 0,
        "provider_matches": 0,
        "created": 0,
        "errors": 0,
    }
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = int(
        damage_store.get_state(
            "zoho_received_cursor_ms",
            int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000),
        )
    )
    # A one-day overlap makes the cursor resilient to search indexing delays.
    since_ms = max(0, cursor - 86_400_000)
    messages = client.search_damage_notifications(since_ms=since_ms)
    stats["messages"] = len(messages)
    drafts = shipping_store.list_drafts(limit=500)
    max_received = cursor
    apaczka_by_tracking: dict[str, dict[str, Any]] | None = None
    apaczka_loaded_successfully = False

    for message in messages:
        received_ms = int(message.get("receivedTime") or message.get("receivedtime") or 0)
        max_received = max(max_received, received_ms)
        sender = str(message.get("fromAddress") or "").strip().lower()
        subject = str(message.get("subject") or "")
        content = str(message.get("content") or message.get("summary") or "")
        if not is_allowed_inpost_sender(sender):
            continue
        if not is_damage_description(f"{subject} {content}"):
            continue
        tracking = extract_inpost_tracking(subject, content)
        if not tracking:
            stats["errors"] += 1
            continue
        message_id = str(message.get("messageId") or "")
        detected_at = datetime.fromtimestamp(received_ms / 1000, timezone.utc).isoformat()
        fingerprint = f"zoho:{message_id}"
        draft = _draft_for_tracking(drafts, tracking)
        existing_case = damage_store.get_case(_case_id(tracking))
        provider_lookup_completed = bool(
            existing_case and existing_case.get("provider_lookup_completed_at")
        )
        provider_context: dict[str, Any] | None = None
        apaczka_order: dict[str, Any] | None = None
        provider_lookup_attempted = False
        if draft is None and apaczka_client is not None and not provider_lookup_completed:
            provider_lookup_attempted = True
            if apaczka_by_tracking is None:
                apaczka_by_tracking = {}
                try:
                    for page in range(1, 11):
                        orders = apaczka_client.list_orders(page=page, limit=25)
                        for order in orders:
                            waybill = str(order.get("waybill_number") or "").strip().upper()
                            if waybill:
                                apaczka_by_tracking[waybill] = order
                            for shipment in order.get("shipments") or []:
                                nested = str(shipment.get("waybill_number") or "").strip().upper()
                                if nested:
                                    apaczka_by_tracking[nested] = order
                        if len(orders) < 25:
                            break
                    apaczka_loaded_successfully = True
                except Exception:
                    logger.exception("Could not list Apaczka orders for damage correlation")
                    stats["errors"] += 1
            apaczka_order = apaczka_by_tracking.get(tracking.upper())
            if apaczka_order:
                reference = str(apaczka_order.get("externalId") or "").strip()
                if reference:
                    draft = next(
                        (
                            item
                            for item in drafts
                            if str(item.get("shopify_order_number") or "").lstrip("#")
                            == reference.lstrip("#")
                            or str(item.get("external_order_id") or "") == reference
                        ),
                        None,
                    )
                receiver = apaczka_order.get("receiver") or {}
                draft_context = _case_context(draft)
                provider_context = {
                    **draft_context,
                    "order_number": draft_context["order_number"] or reference or None,
                    "customer_name": draft_context["customer_name"]
                    or receiver.get("name")
                    or receiver.get("contact_person"),
                    "customer_email": draft_context["customer_email"] or receiver.get("email"),
                    "courier": draft_context["courier"] or "apaczka",
                    "apaczka_order_id": apaczka_order.get("id"),
                }
                stats["provider_matches"] += 1
        case, created = _upsert_detected_case(
            damage_store,
            tracking_number=tracking,
            source="zoho_inpost",
            classification="damage",
            detected_at=detected_at,
            fingerprint=fingerprint,
            evidence={
                "source": "zoho_inpost",
                "message_id": message_id,
                "sender": sender,
                "subject": subject,
                "received_at": detected_at,
                "apaczka_order_id": apaczka_order.get("id") if apaczka_order else None,
                "apaczka_service": (apaczka_order.get("service_name") if apaczka_order else None),
                "has_attachment": str(message.get("hasAttachment", "0")).lower() in {"1", "true"},
            },
            draft=draft,
            provider_context=provider_context,
        )
        if provider_lookup_attempted and apaczka_loaded_successfully:
            damage_store.update_case(
                str(case["id"]),
                {"provider_lookup_completed_at": _now()},
            )
        stats["matched"] += 1
        stats["created"] += int(created)

    # Never advance to the current instant: Zoho documents a short indexing delay.
    damage_store.set_state("zoho_received_cursor_ms", max(max_received, now_ms - 120_000))
    return stats


def build_zoho_client() -> ZohoMailClient | None:
    """Build and authenticate the shared Zoho REST client when configured."""
    client_id = get_secret(KEYCHAIN_SERVICE_ZOHO_CLIENT_ID, required=False)
    client_secret = get_secret(KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET, required=False)
    refresh_token = get_secret(KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN, required=False)
    if not (client_id and client_secret and refresh_token):
        return None
    client = ZohoMailClient(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
    client.authenticate()
    return client


def build_apaczka_lookup_client(storage: Any) -> ApaczkaClient | None:
    """Build a read-only-capable Apaczka client for tracking correlation."""
    app_id = get_secret("apaczka-app-id", required=False)
    app_secret = get_secret("apaczka-app-secret", required=False)
    if not (app_id and app_secret):
        return None
    return ApaczkaClient(app_id, app_secret, service_id="", storage=storage)

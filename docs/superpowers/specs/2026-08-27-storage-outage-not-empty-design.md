# Storage Outage Must Not Look Like Missing Data — Design Spec

**Date:** 2026-08-27
**Status:** Approved
**Issue:** #310
**Scope:** stop `ShippingStore` and `DamageStore` read paths from turning an Azure Table Storage
failure into "there is no such record", and give the API a distinct 503 for it.

---

## Problem

Six read methods across two stores catch bare `Exception` and answer with the value that means
*absence*:

| Method | On failure returns | File |
| --- | --- | --- |
| `ShippingStore.get_draft` | `None` | `zdrovena/common/shipping_store.py:369` |
| `ShippingStore.list_drafts` | `[]` | `zdrovena/common/shipping_store.py:598` |
| `ShippingStore.get_dlq_entry` | `None` | `zdrovena/common/shipping_store.py:571` |
| `ShippingStore.list_dlq` | `[]` | `zdrovena/common/shipping_store.py:557` |
| `DamageStore.get_case` | `None` | `zdrovena/common/damage_store.py:186` |
| `DamageStore.list_cases` | `[]` | `zdrovena/common/damage_store.py:205` |

A timeout, a throttle, a DNS problem, or an expired credential is therefore indistinguishable
from a record that genuinely is not there. To the operator, an Azure outage reads as "the draft
does not exist", "there are no damage cases", "the DLQ is empty".

### The concrete harm

This is not only a display problem. `DamageStore.find_case_by_fingerprint`
(`zdrovena/common/damage_store.py:189`) iterates `list_cases()` and returns `None` when nothing
matches. Under an outage `list_cases()` returns `[]`, the lookup answers "no existing case", and
the caller creates a **duplicate damage case** for an event that was already recorded.

The same shape sits under the shipping sync: a lookup that answers "not found" during an outage
invites a second write for something that already exists. The repository has been bitten by this
class before — `zdrovena/common/shipping_store.py` still carries a comment about roughly 70
duplicated Allegro drafts after an incomplete lookup, which is what issue #316 is about.

### The precedent already in the repository

`zdrovena/common/shopify_dedup_store.py` gets this right today: it imports
`ResourceNotFoundError` from `azure.core.exceptions` and catches *that*, not `Exception`
(lines 246, 262, 302, 310). The fix is to apply the rule the codebase already knows to the six
methods that do not.

---

## Design

### 1. One infrastructure exception

`StorageUnavailableError(ZdrovenaError)` in `zdrovena/common/exceptions.py`.

Deliberately **not** under `ZdrovenaShippingError`. Azure being unreachable is not a fact about a
shipment, and the same stores back Damage and the DLQ. Filing it under the shipping hierarchy
would be a taxonomy that lies to the next reader.

It carries the operation name (`get_draft`, `list_cases`, …) and the underlying exception so logs
can say what failed. The operator never sees the raw Azure response.

### 2. The six read paths stop swallowing

One rule, in two shapes:

- **Single-entity reads** (`get_draft`, `get_dlq_entry`, `get_case`): catch
  `ResourceNotFoundError` and return `None` — that is a genuine absence and the contract is
  unchanged. Any other exception raises `StorageUnavailableError`.
- **List reads** (`list_drafts`, `list_dlq`, `list_cases`): there is no not-found case at all. An
  empty partition yields an empty result without raising, so *every* exception is an outage and
  raises.

### 3. A distinct 503

A new handler in `zdrovena/api/errors.py` for `StorageUnavailableError` returns
**503 Service Unavailable** with an operator-friendly Polish message. `_envelope` already supplies
`correlation_id`, so the acceptance criterion comes for free. The raw exception goes to
`logger.exception`, never into the response.

503 rather than 500 is the point: it tells the operator (and any future alerting) that the system
is unavailable, not that their request was wrong.

### 4. A telemetry signal

`log_event` (`zdrovena/common/events.py`, already masks PII) fires on the outage path with the
store and operation. Issue #214 wants operational alerts; this is the signal they can key on
without the false positives an empty-list metric would produce.

### 5. The local JSON backend

`ShippingStore._local_load_unlocked` (`shipping_store.py:152`) catches a corrupt local store and
returns `{}`. The "file does not exist" case is already handled separately on the line above, so
this branch catches **malformed content only** — and answers it with "there are no drafts".

Same lie, development-only. Included here because it is two lines and a developer hunting "why
are there no drafts" loses the same afternoon an operator would. Called out explicitly because it
sits outside the issue's literal scope: if it should stay, drop this section and nothing else
changes.

---

## Out of scope

- **The claim paths.** `shipping_store.py:277`, `:298`, `:330` return `False` when a claim fails.
  That is fail-safe: a failed claim means "do not proceed", which is the correct behaviour during
  an outage. Converting them to raises would change concurrency semantics for no safety gain.
- **Write paths.** `upsert_draft` and `update_draft` already log and re-raise
  (`shipping_store.py:209-211`, `:240-242`). They are not part of this bug.
- **Retry or circuit-breaking.** This spec makes the failure honest. Deciding to retry it is a
  separate concern, and one the caller is better placed to make.
- **The full-partition scan** behind `list_drafts`. That is issue #316.

---

## Testing

The issue names the cases; each maps to a test.

**Absence still behaves.** `get_draft` on a missing row returns `None`; `list_drafts` on an empty
partition returns `[]`. The contract for genuine not-found is unchanged, and this is the test that
proves the fix did not simply turn every read into an error.

**Outage no longer looks empty.** For each of timeout, throttling (429), and an authentication
failure, every one of the six methods raises `StorageUnavailableError` rather than returning
`None` or `[]`. Parametrised over the exception types `azure.core.exceptions` actually raises.

**The duplicate-case path is closed.** `find_case_by_fingerprint` propagates
`StorageUnavailableError` instead of answering "no existing case", so a caller cannot create a
duplicate during an outage. This is the test that encodes the concrete harm.

**The API answers 503 with a correlation id.** A shipping, a damage, and a DLQ endpoint each
return 503 — not 404, not an empty list — and the envelope carries `correlation_id`. Three
endpoints because the acceptance criterion asks for consistency across all three.

**The raw Azure text does not leak.** The response body contains the Polish message and no
fragment of the underlying exception.

**A corrupt local store is not silently empty.** `_local_load_unlocked` raises on malformed JSON,
while a missing file still returns `{}`.

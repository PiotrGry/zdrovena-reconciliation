# Shipping/Webhooks refactor — working note

> Status: exploration only; no refactor has been approved or started.
>
> Audience: Piotr, Codex, Claude, and future reviewers.
>
> Baseline inspected: `main` at `0a1d4fe` (2026-08-02).

## Purpose

This note is the shared handoff document for reducing the size and coupling of
`zdrovena/api/routers/webhooks.py`. It records the observed problems, candidate
architecture, safety constraints, migration sequence, and open decisions.

Treat this file as a working hypothesis. Update it when repository evidence
invalidates an assumption. Do not use it as permission for a big-bang rewrite.

## Current snapshot

At the inspected baseline, `webhooks.py` has:

- 3,774 lines;
- 90 top-level functions;
- 25 FastAPI route handlers;
- 89 occurrences of `dict[str, Any]` and 108 occurrences of `Any`;
- three courier execution paths: InPost, Apaczka, and Allegro Delivery;
- Shopify webhook verification and order synchronization;
- draft mapping, package calculation, persistence, and merge rules;
- execution claims, preview fingerprints, DLQ recovery, labels, pickup,
  cancellation, fulfillment, manual invoices, and E2E-only support endpoints.

This is a **God Module**, not a God Class. The main problem is not merely file
length; application, domain, provider, persistence, and HTTP concerns depend on
one another through private functions in a router module.

Evidence of the inverted boundary:

- `zdrovena/api/routers/allegro_poller.py` imports `_create_draft` and
  `_sync_draft_from_order` from `webhooks.py`;
- `zdrovena/api/routers/damage.py` imports execution and confirmation helpers
  from `webhooks.py`;
- many tests patch symbols through `zdrovena.api.routers.webhooks.*`, making a
  direct file move unnecessarily disruptive.

## What is already good

The refactor must preserve the useful design work already present:

- preview and execution consume shared provider call specifications;
- a preview fingerprint rejects a stale operator confirmation;
- partial multi-parcel retries skip already persisted shipments;
- execution uses an atomic `executing` claim;
- courier failures are stored in a durable DLQ;
- shipment state transitions have dedicated rules;
- provider clients already isolate HTTP/signing details reasonably well;
- characterization and integration test coverage is extensive.

The goal is to reveal and reinforce these boundaries, not replace working logic.

## Goals

1. Keep FastAPI route handlers thin and transport-only.
2. Give package planning and shipping draft data explicit types.
3. Make adding a courier localized to one adapter plus registration.
4. Keep preview and execution structurally unable to drift.
5. Move reusable application logic out of `api/routers`.
6. Allow provider-specific behavior without a forest of `if courier == ...`.
7. Preserve existing API contracts and stored draft compatibility during the
   migration.

## Non-goals

- No rewrite of `ShippingStore` or Azure Table Storage in the first stages.
- No new workflow engine, ORM, dependency-injection framework, or Unit of Work
  unless a concrete need appears.
- No attempt to make all couriers behave identically. Allegro's asynchronous
  command flow and live delivery proposal are genuine provider differences.
- No endpoint renames or response-shape changes as part of extraction work.
- No reduction of lint, typing, security, or coverage gates.

## Required invariants

Every extraction must keep these behaviors covered:

1. A preview performs no paid/write provider operation.
2. The payload confirmed by an operator is the payload execution will send.
3. A changed draft or sender invalidates the preview fingerprint with HTTP 409.
4. A retry never recreates a parcel already present in `courier_shipments`.
5. Concurrent execution requests cannot create duplicate shipments.
6. A partially successful multi-parcel operation persists each created parcel.
7. A failed execution updates one stable execution-DLQ entry on retry.
8. Provider/domain exceptions keep their existing public HTTP envelopes.
9. InPost and Apaczka intentionally use different sender addresses.
10. Allegro Delivery may report preview as unavailable because its payload
    depends on a live delivery proposal.
11. Manual and automated tracking assignments remain distinguishable.
12. Existing draft records remain readable without a data migration.

## Pattern selection

### 1. Strategy + Adapter: primary pattern

Each courier should expose a provider adapter selected by a registry. Prefer a
small `Protocol` and composition over a deep inheritance hierarchy.

Possible direction (names are provisional):

```python
class CourierAdapter(Protocol):
    key: str
    capabilities: CourierCapabilities

    def plan(
        self,
        draft: ShippingDraft,
        schedule: PickupSchedule | None,
    ) -> ShipmentPlan: ...

    def execute(
        self,
        plan: ShipmentPlan,
        on_created: Callable[[CreatedParcel], None],
    ) -> ShipmentResult: ...
```

Labels, cancellation, and pickup should be capability-based. Do not force a
provider to implement meaningless operations only to satisfy one oversized
interface. Separate protocols such as `LabelProvider` or `CancellableProvider`
may be clearer.

### 2. Builder/Planner: useful, but secondary

A Builder is valuable for constructing an immutable, exact shipment plan. It is
not sufficient as the top-level architecture.

Prefer two explicit stages:

1. `ParcelPlanner`: business package breakdown -> typed physical parcels;
2. provider request builder: draft + parcels -> exact provider requests.

Avoid a ceremonial fluent API such as
`Builder().with_sender(...).with_receiver(...).add_parcel(...)` unless it solves
a demonstrated ordering or validation problem. Pure builders returning typed
dataclasses will be easier to test.

### 3. Application Service: orchestration boundary

`ExecuteShipmentService` should own the cross-cutting workflow currently in
`_execute_draft_impl`:

- load and validate the draft;
- verify the preview fingerprint;
- claim execution atomically;
- select the courier adapter;
- persist partial parcel successes;
- persist the final result;
- write tracking/audit events;
- release the claim and update DLQ on failure.

The service depends on ports (`ShippingRepository`, `CourierRegistry`, event
sink), not on FastAPI. The HTTP route translates exceptions and request bodies.

### 4. Registry/Factory: selection only

A registry should replace repeated provider selection branches:

```python
registry = CourierRegistry(
    inpost=inpost_adapter,
    apaczka=apaczka_adapter,
    allegro_delivery=allegro_adapter,
)
adapter = registry.for_courier(draft.courier)
```

The registry must not grow into a service locator containing unrelated
application dependencies.

### 5. Repository: mostly already present

`ShippingStore` already acts as a repository. A narrow protocol can make the
application layer testable without moving or rewriting storage immediately.

## Proposed domain objects

Introduce types at the application boundary while keeping dict serialization
compatible with the existing store:

- `ShippingDraft`;
- `Receiver` and `Address`;
- `ParcelSpec` and `PhysicalParcel`;
- `PickupSchedule`;
- `PlannedProviderRequest`;
- `ShipmentPlan`;
- `CreatedParcel` and `ShipmentResult`;
- `CourierCapabilities`.

Start with dataclasses or Pydantic models plus `from_record` / `to_record`
conversion. Do not require all historical records to gain every field at once.

`PARCEL_SPECS` and locker constraints should move from
`zdrovena/common/inpost.py` to a provider-neutral package catalog. InPost-only
template information can remain an optional field or live in an InPost mapping.

## Target dependency direction

```text
FastAPI routers
    -> shipping application services
        -> shipping domain models/planners
        -> provider ports and registry
        -> repository/event/DLQ ports
            <- InPost / Apaczka / Allegro adapters
            <- ShippingStore implementation
```

Nothing below the router layer should import from `api.routers`.

## Candidate module layout

```text
zdrovena/shipping/
  domain/
    models.py
    parcel_catalog.py
    planning.py
  application/
    execute_shipment.py
    ingest_order.py
    sync_order.py
  providers/
    protocols.py
    registry.py
    inpost.py
    apaczka.py
    allegro.py

zdrovena/api/routers/
  shopify_webhooks.py
  shipping_drafts.py
  shipping_execution.py
  shipping_dlq.py
  shipping_labels.py
  shipping_invoices.py
  shipping_test_support.py
```

Exact filenames are less important than enforcing dependency direction.

## Mapping from current functions

| Current responsibility | Candidate destination |
| --- | --- |
| `_physical_parcels`, `_calc_packages` | `shipping/domain/planning.py` |
| `PARCEL_SPECS`, locker constraints | `shipping/domain/parcel_catalog.py` |
| `_inpost_call_specs`, `_inpost_payload_plan` | `shipping/providers/inpost.py` |
| `_apaczka_call_specs`, `_apaczka_payload_plan` | `shipping/providers/apaczka.py` |
| `_run_inpost`, `_run_apaczka` | respective provider adapters |
| `_run_allegro_delivery` | Allegro adapter/application sub-service |
| `_execute_draft_impl` | `shipping/application/execute_shipment.py` |
| `_build_draft_record`, merge/sync helpers | ingestion/sync application modules |
| HMAC and Shopify endpoint | `api/routers/shopify_webhooks.py` |
| labels and batch PDF orchestration | `api/routers/shipping_labels.py` plus provider capabilities |
| E2E-only routes | `api/routers/shipping_test_support.py` |

## Incremental migration plan

### Phase 0 — freeze behavior

- Confirm golden tests for InPost, Apaczka, and Allegro payloads.
- Keep endpoint, retry, partial-success, fingerprint, and state-transition tests.
- Add a fitness test preventing `zdrovena/shipping` from importing
  `zdrovena/api/routers`.

Exit criterion: the current behavior is characterized without production code
changes.

### Phase 1 — provider-neutral package domain

- Introduce typed parcel models and package catalog.
- Move pure package/reference calculation behind compatibility wrappers in
  `webhooks.py`.
- Keep serialized draft shapes unchanged.

Exit criterion: all existing tests pass and provider clients no longer own the
shared package catalog.

### Phase 2 — extract provider planning and adapters

- Extract InPost first as the simplest vertical slice.
- Make old `_run_inpost` and preview helpers delegate to the adapter.
- Repeat for Apaczka.
- Treat Allegro separately rather than forcing synchronous preview semantics.

Exit criterion: adding a provider request field requires changing its adapter,
not the router.

### Phase 3 — extract execution application service

- Move claim/fingerprint/DLQ/finalization orchestration.
- Inject the registry and repository protocol.
- Keep the FastAPI endpoint as a thin wrapper.
- Update `damage.py` to call the application service instead of router-private
  functions.

Exit criterion: execution can be tested without FastAPI and no application
module imports `webhooks.py`.

### Phase 4 — extract ingestion and synchronization

- Move draft building, Shopify/Allegro mapping orchestration, and merge rules.
- Update `allegro_poller.py` to call application services.

Exit criterion: pollers and background jobs depend on application modules, not
HTTP routers.

### Phase 5 — split HTTP routers

- Move endpoints by concern while preserving paths and OpenAPI shapes.
- Include the smaller routers from `api/main.py`.
- Retain temporary re-exports only where external imports require them, then
  remove them in a dedicated cleanup.

Exit criterion: `webhooks.py` is removed or reduced to a compatibility facade.

## What not to do

- Do not combine all phases into one PR.
- Do not begin by moving 3,700 lines and fixing imports afterward.
- Do not expose provider SDK payload dictionaries as the domain model.
- Do not make one giant `CourierAdapter` interface covering every optional
  operation.
- Do not duplicate planning between preview and execution.
- Do not delete compatibility wrappers before dependent modules and tests move.
- Do not loosen quality gates to make structural work easier to merge.

## Open decisions

1. Should Allegro preview perform a read-only live proposal call, or remain
   explicitly unavailable?
2. Should provider request payloads be stored in `ShipmentPlan`, or generated
   deterministically from typed plan fields immediately before execution?
3. Should the persisted draft gain a schema version during this refactor, or be
   postponed until a concrete storage migration is needed?
4. Should label, cancellation, and pickup use separate capability protocols or
   explicit application commands?
5. How long should compatibility exports remain in `webhooks.py`?

## Suggested first implementation issue

**Extract provider-neutral parcel catalog and typed shipment plan without
changing runtime behavior.**

Scope:

- add parcel/domain types;
- relocate the shared package catalog;
- make current planning helpers delegate to the new module;
- add parity and architecture-boundary tests;
- no endpoint, provider API, storage, or deployment changes.

This gives the later adapter extraction a stable vocabulary and is small enough
to review independently.

## Agent handoff protocol

Before implementing a phase, Codex or Claude should:

1. read this document and the relevant existing design docs;
2. inspect the current `main` because line numbers and assumptions will drift;
3. write or identify characterization tests before moving behavior;
4. create one issue/branch for one phase;
5. record material decisions and rejected alternatives below;
6. keep public API and stored-record compatibility explicit in the PR body.

## Decision log

| Date | Decision | Status |
| --- | --- | --- |
| 2026-08-02 | Diagnose `webhooks.py` as a God Module rather than a God Class. | Accepted |
| 2026-08-02 | Prefer Strategy/Adapter + Application Service; use Builder only for deterministic shipment planning. | Proposed |
| 2026-08-02 | Refactor incrementally with compatibility wrappers and unchanged API contracts. | Proposed |

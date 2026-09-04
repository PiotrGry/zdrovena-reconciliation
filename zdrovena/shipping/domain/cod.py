"""Split one collection amount across the physical parcels of a draft.

One physical parcel is one carrier shipment, and every shipment carries its own
``cod`` object, so a multi-parcel COD order has to say how much each parcel
collects. Anything that does not add up to what the customer still owes either
loses money or charges it twice, so the arithmetic here is integer grosze from
end to end and the parts are proved to sum to the total before they are
returned.

Nothing is stored: the split is a function of the amount, the shipping price,
the order lines and the parcel plan. The operator repacking a draft therefore
changes the split by itself, and there is no cached copy to go stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from zdrovena.common.bottles import SKIP_RE, is_glass
from zdrovena.common.shipping_parcels import PARCEL_HALF_PACKS
from zdrovena.shipping.domain.models import PhysicalParcel
from zdrovena.shipping.domain.planning import (
    half_packs_for_item,
    physical_parcels,
    product_name,
)

# Boxes that hold glass. Glass and plastic are packed separately, so their
# money has to be kept apart too.
GLASS_PACKAGE_TYPES = frozenset({"szkło", "szkło-2pak"})

# What cod_allocation used to weigh the parcels.
BASIS_VALUE = "value"
BASIS_EQUAL = "equal"


class CodAllocationError(ValueError):
    """The COD amount cannot be split safely across this draft's parcels."""


@dataclass(frozen=True)
class CodAllocation:
    """Per-parcel collection amounts, aligned to ``physical_parcels(draft)``."""

    amounts: tuple[Decimal, ...]
    basis: str

    def __len__(self) -> int:
        return len(self.amounts)


def _grosze(raw: Any, field: str) -> int:
    """Read a money string as whole grosze, refusing anything unrepresentable."""
    try:
        amount = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CodAllocationError(f"Invalid {field}: {raw!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise CodAllocationError(f"Invalid {field}: {raw!r}")
    if amount != amount.quantize(Decimal("0.01")):
        raise CodAllocationError(f"{field} has sub-grosz precision: {raw!r}")
    return int(amount * 100)


def _largest_remainder(total_gr: int, weights: list[Fraction]) -> list[int]:
    """Split whole grosze by weight so the parts sum to the total exactly.

    Ties go to the earlier parcel, which makes the result reproducible: a
    resume after a partial failure has to land on the same numbers as the run
    that created the first label.
    """
    count = len(weights)
    if total_gr == 0:
        return [0] * count
    denominator = sum(weights)
    exact = [Fraction(total_gr) * weight / denominator for weight in weights]
    parts = [int(value) for value in exact]
    spare = total_gr - sum(parts)
    order = sorted(range(count), key=lambda index: (parts[index] - exact[index], index))
    for index in order[:spare]:
        parts[index] += 1
    return parts


def _capacity(package_type: str) -> int:
    return PARCEL_HALF_PACKS.get(package_type, PARCEL_HALF_PACKS["1-pak"])


def _packable_lines(draft: dict[str, Any]) -> list[tuple[int, int, bool]]:
    """Return ``(line_index, half_packs, is_glass)`` for lines that occupy a box.

    SKIP_RE lines are dropped the same way the planner drops them. It matters
    here as well as there: "Kaucja za butelki" carries no bottle count, so the
    unreadable-name fallback would size it as two half-packs per unit and hand
    a deposit line a parcel of its own.
    """
    lines = []
    for index, item in enumerate(draft.get("order_items") or []):
        name = product_name(item)
        if SKIP_RE.search(name):
            continue
        half_packs = half_packs_for_item(item)
        if half_packs > 0:
            lines.append((index, half_packs, is_glass(name)))
    return lines


def _assign_half_packs(
    draft: dict[str, Any], parcels: list[PhysicalParcel]
) -> list[dict[int, int]]:
    """Map order lines onto parcels, returning half-packs taken per line.

    Lines are packed in order, glass into glass boxes and everything else into
    the rest, which is how they are packed on the bench. Two things are normal
    rather than errors: a box can end up partly empty, because the planner
    rounds glass up to a whole box, and goods can outlast the boxes when the
    operator repacks into fewer of them — the last box of that material then
    takes the remainder.
    """
    queues: dict[bool, list[list[int]]] = {True: [], False: []}
    for index, half_packs, glass in _packable_lines(draft):
        queues[glass].append([index, half_packs])

    shares: list[dict[int, int]] = [{} for _ in parcels]
    for position, parcel in enumerate(parcels):
        glass = parcel.package_type in GLASS_PACKAGE_TYPES
        queue = queues[glass]
        remaining = _capacity(parcel.package_type)
        while remaining > 0 and queue:
            line_index, available = queue[0]
            taken = min(remaining, available)
            shares[position][line_index] = shares[position].get(line_index, 0) + taken
            remaining -= taken
            if taken < available:
                queue[0][1] = available - taken
            else:
                queue.pop(0)

    for glass, queue in queues.items():
        if not queue:
            continue
        matching = [
            position
            for position, parcel in enumerate(parcels)
            if (parcel.package_type in GLASS_PACKAGE_TYPES) is glass
        ]
        target = matching[-1] if matching else len(parcels) - 1
        for line_index, remainder in queue:
            shares[target][line_index] = shares[target].get(line_index, 0) + remainder
    return shares


def _parcel_weights(
    draft: dict[str, Any], parcels: list[PhysicalParcel]
) -> tuple[list[Fraction], str]:
    """Return one weight per parcel, and which basis produced it.

    A draft written before line values were persisted has nothing to weigh, so
    it falls back to an equal split. The customer pays the same total either
    way; only the distribution between parcels is a guess, which is why the
    basis is reported rather than hidden.
    """
    equal = ([Fraction(1)] * len(parcels), BASIS_EQUAL)
    lines = _packable_lines(draft)
    items = draft.get("order_items") or []
    values: dict[int, int] = {}
    for index, _half_packs, _glass in lines:
        item = items[index]
        raw = item.get("line_total")
        if raw is None:
            return equal
        values[index] = _grosze(raw, f"line total for {product_name(item) or index}")

    totals = {index: half_packs for index, half_packs, _glass in lines}
    weights = []
    for share in _assign_half_packs(draft, parcels):
        weight = sum(
            (Fraction(values[index] * taken, totals[index]) for index, taken in share.items()),
            Fraction(0),
        )
        weights.append(weight)
    if sum(weights) == 0:
        return equal
    return weights, BASIS_VALUE


def cod_allocation(draft: dict[str, Any]) -> CodAllocation:
    """Split this draft's COD amount across its physical parcels."""
    cod = draft.get("cod")
    if not cod:
        raise CodAllocationError("Cannot split a draft with no COD amount")
    total_gr = _grosze(cod.get("amount"), "COD amount")
    shipping_gr = _grosze(draft.get("shipping_price") or "0", "shipping price")
    if shipping_gr > total_gr:
        raise CodAllocationError(
            f"Shipping price {shipping_gr / 100:.2f} exceeds the collected "
            f"amount {total_gr / 100:.2f} — nothing left to split across parcels"
        )

    parcels = physical_parcels(draft)
    weights, basis = _parcel_weights(draft, parcels)
    shipping_parts = _largest_remainder(shipping_gr, [Fraction(1)] * len(parcels))
    goods_parts = _largest_remainder(total_gr - shipping_gr, weights)

    parts = [shipping + goods for shipping, goods in zip(shipping_parts, goods_parts, strict=True)]
    if sum(parts) != total_gr:  # pragma: no cover - guarded by construction
        raise CodAllocationError(
            f"Split of {total_gr} grosze produced {sum(parts)} — refusing to send it"
        )
    empty = [position + 1 for position, part in enumerate(parts) if part == 0]
    if empty:
        raise CodAllocationError(
            f"Parcel {empty[0]} of {len(parts)} would collect 0.00 — "
            "the carrier rejects that, so repack the order or drop the empty box"
        )
    amounts = tuple((Decimal(part) / 100).quantize(Decimal("0.01")) for part in parts)
    return CodAllocation(amounts=amounts, basis=basis)

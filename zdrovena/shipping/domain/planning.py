"""Pure parcel planning behavior with no API, storage, or provider dependencies."""

from __future__ import annotations

from math import ceil
from typing import Any

from zdrovena.common.bottles import bottles_per_unit, is_glass
from zdrovena.common.shipping_parcels import _DEFAULT_DIMS, LOCKER_LARGE_SLOT, PARCEL_SPECS
from zdrovena.shipping.domain.models import (
    PackageBreakdownItem,
    PackagePlan,
    ParcelSpec,
    PhysicalParcel,
)

GLASS_PACKAGE_SIZES = {
    "szkło": "1-pak",
    "szkło-2pak": "2-pak",
}


def product_name(item: dict[str, Any]) -> str:
    """Read a line item's product name the one way the whole pipeline reads it.

    Shopify sends `name`; the Allegro mapper sends both. Reading `name` in one
    place and `name or title` in another let a title-only line pass the
    readability guard and still be planned from an empty string.
    """
    return str(item.get("name") or item.get("title") or "").strip()


def calc_packages(product_items: list[dict[str, Any]]) -> PackagePlan:
    """Calculate an ordered parcel plan for filtered product line items."""
    plastic_half_packs = 0
    glass_half_packs = 0
    for item in product_items:
        qty = item.get("quantity", 1)
        name = product_name(item)
        bottle_count = bottles_per_unit(name)
        half_packs = ceil(float(qty) * bottle_count / 6) if bottle_count else int(qty) * 2
        if is_glass(name):
            glass_half_packs += half_packs
        else:
            plastic_half_packs += half_packs

    breakdown: list[PackageBreakdownItem] = []

    remaining = plastic_half_packs // 2
    for box_size, label in ((3, "3-pak"), (2, "2-pak"), (1, "1-pak")):
        if remaining >= box_size:
            count = remaining // box_size
            breakdown.append(PackageBreakdownItem(package_type=label, quantity=count))
            remaining -= count * box_size
    if plastic_half_packs % 2:
        breakdown.append(PackageBreakdownItem(package_type="pół-pak", quantity=1))

    remaining_glass = (glass_half_packs + 1) // 2
    if remaining_glass >= 2:
        count = remaining_glass // 2
        breakdown.append(PackageBreakdownItem(package_type="szkło-2pak", quantity=count))
        remaining_glass -= count * 2
    if remaining_glass > 0:
        breakdown.append(PackageBreakdownItem(package_type="szkło", quantity=remaining_glass))

    total = sum(item.quantity for item in breakdown)
    return PackagePlan(package_count=max(total, 1), breakdown=tuple(breakdown))


def unreadable_product_names(product_items: list[dict[str, Any]]) -> list[str]:
    """Return named product lines whose bottle count cannot be read.

    ``calc_packages`` falls back to "one unit is one zgrzewka" for these, which
    is a guess. When the shop renamed the glass SKU to "... 12 szt." the guess
    happened to be right about the box count and silently wrong about the
    material (orders #1710-#1712). A named line we cannot read is therefore an
    operator review, not a silent assumption. Unnamed lines keep the fallback:
    a missing name carries no information a rename could have changed.
    """
    unreadable = []
    for item in product_items:
        name = product_name(item)
        if name and bottles_per_unit(name) == 0:
            unreadable.append(name)
    return unreadable


def physical_parcels(draft: dict[str, Any]) -> list[PhysicalParcel]:
    """Expand a legacy package breakdown into individual physical parcels."""
    parcels: list[PhysicalParcel] = []
    for box in draft.get("packages_breakdown") or []:
        package_type = str(box.get("type") or "1-pak")
        quantity = int(box.get("qty") or 1)
        parcels.extend(
            PhysicalParcel(
                package_type=package_type,
                position=position,
                count_for_type=quantity,
            )
            for position in range(1, quantity + 1)
        )
    return parcels or [PhysicalParcel(package_type="1-pak", position=1, count_for_type=1)]


def parcel_weight_and_dims(draft: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Derive total weight and the raw largest-box catalogue record."""
    breakdown = draft.get("packages_breakdown") or []
    total_weight = 0.0
    largest_dims = _DEFAULT_DIMS
    largest_volume = 0.0

    for box in breakdown:
        box_type = box.get("type", "")
        qty = box.get("qty", 1)
        spec_record = PARCEL_SPECS.get(box_type)
        if not spec_record:
            continue
        spec = ParcelSpec.from_record(spec_record)
        total_weight += spec.weight_kg * qty
        volume = spec.length * spec.width * spec.height
        if volume > largest_volume:
            largest_volume = volume
            largest_dims = spec_record

    return (total_weight if total_weight > 0 else 6.0), largest_dims


def shipment_reference(
    order_number: str,
    package_type: str,
    package_number: int,
    package_count: int,
) -> str:
    """Build the legacy courier reference for one physical parcel."""
    material, size = _parcel_material_and_size(package_type)
    reference = f"{order_number} | {material} | {size}"
    if package_count > 1:
        reference = f"{reference} {package_number}/{package_count}"
    return reference


def parcel_content(package_type: str) -> str:
    """Describe one physical parcel for carriers that require a content line.

    Apaczka caps `order.content` at 50 characters, so the description is built
    from the parcel itself rather than from product names: a catalogue rename
    must never be able to push this over the carrier's limit.
    """
    material, size = _parcel_material_and_size(package_type)
    return f"Woda butelkowana, {material} {size}"


def _parcel_material_and_size(package_type: str) -> tuple[str, str]:
    """Split a package type into the material and pack size shown to couriers."""
    material = "szkło" if package_type in GLASS_PACKAGE_SIZES else "plastik"
    return material, GLASS_PACKAGE_SIZES.get(package_type, package_type)


def package_fit_warnings(
    breakdown: list[dict[str, Any]],
    *,
    carrier: str = "inpost",
) -> list[str]:
    """Return warnings for boxes exceeding a carrier's largest locker slot."""
    slot = LOCKER_LARGE_SLOT.get(carrier)
    if not slot:
        return []
    warnings: list[str] = []
    for box in breakdown:
        box_type = box.get("type", "")
        spec_record = PARCEL_SPECS.get(box_type)
        if not spec_record:
            continue
        spec = ParcelSpec.from_record(spec_record)
        pkg_sides = sorted([spec.length, spec.width, spec.height])
        slot_sides = sorted([slot["height"], slot["width"], slot["depth"]])
        if any(package > limit for package, limit in zip(pkg_sides, slot_sides, strict=True)):
            warnings.append(
                f"box '{box_type}' ({spec.length}×{spec.width}×{spec.height} cm) "
                f"exceeds {carrier} locker large slot "
                f"({slot['height']}×{slot['width']}×{slot['depth']} cm)"
            )
        if spec.weight_kg > slot["max_weight_kg"]:
            warnings.append(
                f"box '{box_type}' weight {spec.weight_kg} kg exceeds "
                f"{carrier} locker max {slot['max_weight_kg']} kg"
            )
    return warnings

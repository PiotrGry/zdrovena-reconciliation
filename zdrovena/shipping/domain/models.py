"""Value objects used by pure parcel planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ParcelSpec:
    """Typed view of one entry in the canonical mutable parcel catalogue."""

    length: int | float
    width: int | float
    height: int | float
    weight_kg: int | float
    paczkomat_template: str | None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ParcelSpec:
        return cls(
            length=record["length"],
            width=record["width"],
            height=record["height"],
            weight_kg=record["weight_kg"],
            paczkomat_template=record.get("paczkomat_template"),
        )


@dataclass(frozen=True, slots=True)
class PackageBreakdownItem:
    """One package type and its quantity in a calculated package plan."""

    package_type: str
    quantity: int

    def to_legacy_dict(self) -> dict[str, Any]:
        return {"type": self.package_type, "qty": self.quantity}


@dataclass(frozen=True, slots=True)
class PhysicalParcel:
    """One physical parcel expanded from a package breakdown entry."""

    package_type: str
    position: int
    count_for_type: int

    def to_legacy_tuple(self) -> tuple[str, int, int]:
        return self.package_type, self.position, self.count_for_type


@dataclass(frozen=True, slots=True)
class PackagePlan:
    """Calculated parcel count and ordered package breakdown."""

    package_count: int
    breakdown: tuple[PackageBreakdownItem, ...]

    def to_legacy_tuple(self) -> tuple[int, list[dict[str, Any]]]:
        return self.package_count, [item.to_legacy_dict() for item in self.breakdown]

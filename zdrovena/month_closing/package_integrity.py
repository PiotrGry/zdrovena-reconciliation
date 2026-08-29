"""Proof that the package being sent is the package that was reviewed.

Month-close recorded a blob key and a file list. Send re-fetched the blob by
that key, so whatever could write to the key between review and send decided
what left the building -- and nothing recorded that a swap had happened
(issue #311).

The artefact now carries a SHA-256 of the bytes, their size, and a sorted
manifest. The hash doubles as the immutable artifact id: an id derived from
content cannot name two different payloads, which is exactly the property a
generated uuid would not have.

Verification is deliberately fail-closed. Every uncertainty -- blob gone, store
unreachable, artefact recorded before hashing existed -- refuses the send.
Rebuilding a package costs one click; mailing the accountant the wrong month's
documents costs considerably more.
"""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from typing import Any

logger = logging.getLogger("zdrovena.month_closing.package_integrity")

_ARTIFACT_ID_PREFIX = "sha256-"


class PackageIntegrityError(RuntimeError):
    """The package artefact could not be built from what is in storage."""


def _read_blob(storage: Any, key: str) -> bytes:
    return b"".join(storage.stream(key))


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_package_artifact(storage: Any, *, key: str, files: list[str]) -> dict[str, Any]:
    """Record what was built, strongly enough to recognise it later.

    The manifest is sorted so that a re-run listing the same documents in a
    different order does not read as a different package.
    """
    try:
        payload = _read_blob(storage, key)
    except Exception as exc:
        raise PackageIntegrityError(f"Cannot read the package just built at {key}: {exc}") from exc

    sha256 = _digest(payload)
    return {
        "kind": "package",
        "key": key,
        "sha256": sha256,
        "size_bytes": len(payload),
        "artifact_id": f"{_ARTIFACT_ID_PREFIX}{sha256[:16]}",
        "files": sorted(files),
    }


def verify_package_artifact(storage: Any, artifact: dict[str, Any] | None) -> str | None:
    """Return None when the stored package still matches, else why it does not.

    The message is operator-facing and Polish, matching the other blocking
    reasons in the month-close workflow.
    """
    if not artifact:
        return "Brak zapisanej paczki — zbuduj ją ponownie przed wysyłką."

    key = artifact.get("key")
    if not key:
        return "Zapisana paczka nie ma klucza w magazynie — zbuduj ją ponownie."

    expected_hash = artifact.get("sha256")
    if not expected_hash:
        # Runs packaged before hashing existed. Refusing is the safe answer.
        return (
            "Zapisana paczka nie ma sumy kontrolnej (pochodzi sprzed wprowadzenia "
            "weryfikacji) — zbuduj ją ponownie przed wysyłką."
        )

    try:
        payload = _read_blob(storage, str(key))
    except Exception as exc:
        # Covers both "blob is gone" and "storage is unreachable". Neither is
        # evidence that the package is intact, so neither may pass.
        logger.warning("Package verification could not read %s: %s", key, exc)
        return f"Nie udało się odczytać paczki {key} do weryfikacji — wysyłka wstrzymana."

    actual_hash = _digest(payload)
    if actual_hash != expected_hash:
        logger.error(
            "Package hash mismatch for %s: reviewed %s, found %s", key, expected_hash, actual_hash
        )
        return (
            "Paczka w magazynie zmieniła się od czasu przeglądu "
            f"(suma kontrolna {actual_hash[:16]}… zamiast {str(expected_hash)[:16]}…) — "
            "zbuduj ją ponownie i przejrzyj przed wysyłką."
        )

    expected_size = artifact.get("size_bytes")
    if expected_size is not None and int(expected_size) != len(payload):
        return "Rozmiar paczki nie zgadza się z zapisanym — wysyłka wstrzymana."

    manifest_reason = _verify_manifest(payload, artifact.get("files"))
    if manifest_reason:
        return manifest_reason

    return None


def _verify_manifest(payload: bytes, recorded_files: Any) -> str | None:
    """Check the recorded document list against the archive's own entries.

    Comparing the manifest to itself would prove nothing -- a doctored list is
    still a sorted list. The archive names it actually contains are the only
    independent witness available here.

    This does not defend against an attacker who can rewrite the run state and
    the blob together; the hash above is what covers substitution of the bytes.
    What it catches is the recorded manifest drifting away from the artefact,
    which is what an operator is shown when they review.
    """
    if recorded_files is None:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            actual = sorted(name for name in archive.namelist() if not name.endswith("/"))
    except zipfile.BadZipFile:
        # Hash already pins the bytes; a non-archive payload has no entry list
        # to compare against.
        return None

    if sorted(str(name) for name in recorded_files) != actual:
        return (
            "Manifest dokumentów nie zgadza się z zawartością paczki — "
            "zbuduj ją ponownie i przejrzyj przed wysyłką."
        )
    return None

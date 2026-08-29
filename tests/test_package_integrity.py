"""The package that gets sent must be the package that was reviewed (#311).

Month-close stored only a blob key and a file list. Send re-fetched the blob by
that key, so anything that could write to the key between review and send chose
what left the building -- and nothing recorded that it had happened.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from zdrovena.month_closing.package_integrity import (
    PackageIntegrityError,
    build_package_artifact,
    verify_package_artifact,
)


class _FakeStorage:
    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.blobs = dict(blobs or {})

    def exists(self, key: str) -> bool:
        return key in self.blobs

    def stream(self, key: str):
        if key not in self.blobs:
            raise FileNotFoundError(key)
        yield self.blobs[key]


def _zip(*names: str) -> bytes:
    """A real archive: the manifest check compares against its actual entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name in names:
            archive.writestr(name, b"content of " + name.encode())
    return buf.getvalue()


ZIP_BYTES = _zip("a.pdf", "b.pdf")


class TestBuildPackageArtifact:
    def test_records_hash_size_and_manifest(self):
        storage = _FakeStorage({"faktury/2026/pak.zip": ZIP_BYTES})

        artifact = build_package_artifact(
            storage, key="faktury/2026/pak.zip", files=["b.pdf", "a.pdf"]
        )

        assert artifact["kind"] == "package"
        assert artifact["key"] == "faktury/2026/pak.zip"
        assert artifact["size_bytes"] == len(ZIP_BYTES)
        assert len(artifact["sha256"]) == 64
        assert artifact["files"] == ["a.pdf", "b.pdf"], "manifest is order-independent"

    def test_the_artifact_id_is_the_content_hash(self):
        """An immutable id derived from content cannot name two different bytes."""
        storage = _FakeStorage({"k.zip": ZIP_BYTES})

        artifact = build_package_artifact(storage, key="k.zip", files=[])

        assert artifact["artifact_id"].endswith(artifact["sha256"][:16])

    def test_the_same_bytes_always_produce_the_same_hash(self):
        a = build_package_artifact(_FakeStorage({"x": ZIP_BYTES}), key="x", files=[])
        b = build_package_artifact(_FakeStorage({"y": ZIP_BYTES}), key="y", files=[])

        assert a["sha256"] == b["sha256"]

    def test_a_missing_blob_cannot_be_recorded_as_a_package(self):
        with pytest.raises(PackageIntegrityError):
            build_package_artifact(_FakeStorage(), key="gone.zip", files=[])


class TestVerifyPackageArtifact:
    def _artifact(self, storage, key="faktury/2026/pak.zip", files=("a.pdf", "b.pdf")):
        return build_package_artifact(storage, key=key, files=list(files))

    def test_untouched_package_verifies(self):
        storage = _FakeStorage({"faktury/2026/pak.zip": ZIP_BYTES})
        artifact = self._artifact(storage)

        assert verify_package_artifact(storage, artifact) is None

    def test_swapped_bytes_are_refused(self):
        """The case this issue exists for."""
        storage = _FakeStorage({"faktury/2026/pak.zip": ZIP_BYTES})
        artifact = self._artifact(storage)
        storage.blobs["faktury/2026/pak.zip"] = _zip("a.pdf", "b.pdf", "smuggled.pdf")

        reason = verify_package_artifact(storage, artifact)

        assert reason is not None
        assert "zmieni" in reason.lower() or "hash" in reason.lower()

    def test_a_deleted_package_is_refused(self):
        storage = _FakeStorage({"faktury/2026/pak.zip": ZIP_BYTES})
        artifact = self._artifact(storage)
        del storage.blobs["faktury/2026/pak.zip"]

        assert verify_package_artifact(storage, artifact) is not None

    def test_a_manifest_that_disagrees_with_the_archive_is_refused(self):
        """The recorded list is what the operator reviewed; the archive entries
        are what would actually be sent."""
        storage = _FakeStorage({"faktury/2026/pak.zip": ZIP_BYTES})
        artifact = self._artifact(storage)
        artifact["files"] = ["a.pdf", "smuggled.pdf"]

        assert verify_package_artifact(storage, artifact) is not None

    def test_an_artifact_without_a_hash_is_refused(self):
        """Runs packaged before this existed have no hash. Refusing is the
        safe answer: repackaging is cheap, sending the wrong ZIP is not."""
        storage = _FakeStorage({"faktury/2026/pak.zip": ZIP_BYTES})

        reason = verify_package_artifact(
            storage, {"kind": "package", "key": "faktury/2026/pak.zip", "files": []}
        )

        assert reason is not None

    def test_a_missing_artifact_is_refused(self):
        assert verify_package_artifact(_FakeStorage(), None) is not None

    def test_an_unreadable_store_is_refused_not_passed(self):
        class _Broken(_FakeStorage):
            def stream(self, key: str):
                raise RuntimeError("storage unavailable")

        storage = _FakeStorage({"k.zip": ZIP_BYTES})
        artifact = self._artifact(storage, key="k.zip")
        broken = _Broken({"k.zip": ZIP_BYTES})

        assert verify_package_artifact(broken, artifact) is not None

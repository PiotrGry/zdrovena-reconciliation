"""The Files API must not reach internal state (issue #311).

The same blob container holds documents an operator manages and artefacts the
workflows own. A generic PUT/DELETE over the whole container means a mistyped
key can silently destroy internal state, and no role check helps -- the operator
is *supposed* to have the accountant role.

The rules here are deliberately narrow. Blocking all of `faktury/` would be
simpler and wrong: uploading missing invoices under that prefix IS the operator's
job. The package artefact is protected by hash verification instead.
"""

from __future__ import annotations

import pytest

from zdrovena.common.storage_namespaces import (
    SystemKeyError,
    is_system_key,
    normalise_storage_key,
    require_user_key,
)


class TestNormalisation:
    """Every bypass has to fail on the normalised form, not the raw string."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("faktury/2026/faktura.pdf", "faktury/2026/faktura.pdf"),
            ("/faktury/x.pdf", "faktury/x.pdf"),
            ("faktury//x.pdf", "faktury/x.pdf"),
            ("faktury\\2026\\x.pdf", "faktury/2026/x.pdf"),
            ("apaczka%2Fservice_structure.json", "apaczka/service_structure.json"),
            ("apaczka%252Fservice_structure.json", "apaczka/service_structure.json"),
            # Case is preserved: blob names are case-sensitive, and this is
            # the key the caller then reads and writes.
            ("Faktury/2026/Kwiecień/Faktura.PDF", "Faktury/2026/Kwiecień/Faktura.PDF"),
            ("  faktury/x.pdf  ", "faktury/x.pdf"),
        ],
    )
    def test_variants_collapse_to_one_form(self, raw, expected):
        assert normalise_storage_key(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "..", "a/../b", "%2e%2e/x", "a/./b/../c"])
    def test_traversal_and_emptiness_are_rejected(self, raw):
        with pytest.raises(SystemKeyError):
            normalise_storage_key(raw)


class TestSystemKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "apaczka/service_structure.json",
            "apaczka/points/parcel_locker.json",
            "APACZKA/service_structure.json",
            "apaczka%2Fservice_structure.json",
            "apaczka%252Fpoints%252Fx.json",
            "faktury/2026/Kwiecień/.state.json",
            "faktury/2026/Kwiecień/.file_hashes.json",
            ".state.json",
            "some/dir/.hidden/file.pdf",
        ],
    )
    def test_internal_artefacts_are_system_owned(self, key):
        assert is_system_key(key) is True
        with pytest.raises(SystemKeyError):
            require_user_key(key)

    @pytest.mark.parametrize(
        "key",
        [
            "faktury/2026/Kwiecień/faktura-001.pdf",
            "faktury/2026/Kwiecień/Kwiecień_2026_HUMIO.zip",
            "inbox/wyciag.pdf",
            "manual-documents/umowa.pdf",
            "apaczka-manual/notatka.pdf",
            "faktury/apaczka/nota.pdf",
        ],
    )
    def test_operator_documents_stay_writable(self, key):
        """Blocking these would break the workflow the Files UI exists for."""
        assert is_system_key(key) is False
        assert require_user_key(key) == normalise_storage_key(key)

    def test_a_prefix_match_is_not_a_segment_match(self):
        """`apaczka-manual/` merely starts with the protected prefix."""
        assert is_system_key("apaczka-manual/x.pdf") is False

    def test_require_user_key_returns_the_normalised_form(self):
        """Callers must persist what was checked, not the raw input."""
        assert require_user_key("/faktury//2026/x.pdf") == "faktury/2026/x.pdf"

    def test_case_survives_the_round_trip(self):
        """Lower-casing the returned key would break every Polish month folder."""
        key = "faktury/2026/Kwiecień/Kwiecień_2026_HUMIO.zip"

        assert require_user_key(key) == key

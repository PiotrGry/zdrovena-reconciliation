"""Tests for _pick_courier / _pick_inpost_service ENV-driven mapping (P1-7)."""

from __future__ import annotations

import pytest

from zdrovena.api.routers.webhooks import (
    _parse_title_map,
    _pick_apaczka_service,
    _pick_courier,
    _pick_inpost_service,
    _reset_courier_maps_cache,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ENV + cache between tests."""
    monkeypatch.delenv("COURIER_TITLE_MAP", raising=False)
    monkeypatch.delenv("INPOST_SERVICE_TITLE_MAP", raising=False)
    monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
    _reset_courier_maps_cache()
    yield
    _reset_courier_maps_cache()


# ── _parse_title_map ─────────────────────────────────────────────────────────


class TestParseTitleMap:
    def test_empty_string_returns_empty(self) -> None:
        assert _parse_title_map("") == {}
        assert _parse_title_map("   ") == {}

    def test_json_format(self) -> None:
        assert _parse_title_map('{"inpost": "inpost", "DPD": "apaczka"}') == {
            "inpost": "inpost",
            "dpd": "apaczka",
        }

    def test_json_non_object_returns_empty(self) -> None:
        assert _parse_title_map("[1,2,3]") == {}

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_title_map("{not-json") == {}

    def test_semicolon_pairs(self) -> None:
        assert _parse_title_map("inpost=inpost;paczkomat=inpost;dpd=apaczka") == {
            "inpost": "inpost",
            "paczkomat": "inpost",
            "dpd": "apaczka",
        }

    def test_comma_pairs_also_accepted(self) -> None:
        assert _parse_title_map("inpost=inpost,dpd=apaczka") == {
            "inpost": "inpost",
            "dpd": "apaczka",
        }

    def test_lowercases_keys(self) -> None:
        assert _parse_title_map("InPost=inpost") == {"inpost": "inpost"}

    def test_ignores_malformed_pairs(self) -> None:
        assert _parse_title_map("inpost;=x;valid=y;=") == {"valid": "y"}


# ── _pick_courier ────────────────────────────────────────────────────────────


class TestPickCourierFallback:
    """Substring heuristics preserved when ENV unset (backwards compat)."""

    def test_inpost_keyword_routes_to_inpost(self) -> None:
        assert _pick_courier({"shipping_lines": [{"title": "InPost Paczkomat"}]}) == "inpost"

    def test_paczkomat_keyword_routes_to_inpost(self) -> None:
        assert _pick_courier({"shipping_lines": [{"title": "Paczkomat 24/7"}]}) == "inpost"

    def test_kurier_alone_routes_to_apaczka(self) -> None:
        assert _pick_courier({"shipping_lines": [{"title": "Kurier DPD"}]}) == "apaczka"

    def test_empty_shipping_lines_routes_to_apaczka(self) -> None:
        assert _pick_courier({}) == "apaczka"
        assert _pick_courier({"shipping_lines": []}) == "apaczka"


class TestPickCourierExplicitMap:
    def test_env_mapping_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COURIER_TITLE_MAP", "dpd=apaczka;inpost=inpost")
        _reset_courier_maps_cache()
        assert _pick_courier({"shipping_lines": [{"title": "DPD Standard"}]}) == "apaczka"

    def test_env_mapping_can_override_default_heuristic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # operator points 'inpost' at apaczka (unusual but explicit)
        monkeypatch.setenv("COURIER_TITLE_MAP", "inpost=apaczka")
        _reset_courier_maps_cache()
        assert _pick_courier({"shipping_lines": [{"title": "InPost Kurier"}]}) == "apaczka"

    def test_env_mapping_supports_new_courier_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COURIER_TITLE_MAP", "gls=apaczka;fedex=apaczka")
        _reset_courier_maps_cache()
        assert _pick_courier({"shipping_lines": [{"title": "GLS ekspres"}]}) == "apaczka"

    def test_env_mapping_falls_back_to_heuristic_on_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COURIER_TITLE_MAP", "gls=apaczka")
        _reset_courier_maps_cache()
        # title matches no ENV key → heuristic still routes InPost → inpost
        assert _pick_courier({"shipping_lines": [{"title": "InPost paczkomat"}]}) == "inpost"

    def test_json_env_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "COURIER_TITLE_MAP",
            '{"dpd": "apaczka", "paczkomat": "inpost"}',
        )
        _reset_courier_maps_cache()
        assert _pick_courier({"shipping_lines": [{"title": "Paczkomat"}]}) == "inpost"
        assert _pick_courier({"shipping_lines": [{"title": "DPD kurier"}]}) == "apaczka"


# ── _pick_inpost_service ─────────────────────────────────────────────────────


class TestPickInpostServiceFallback:
    def test_paczkomat_default(self) -> None:
        assert _pick_inpost_service("InPost Paczkomat 24/7") == "paczkomat"

    def test_kurier_default(self) -> None:
        assert _pick_inpost_service("InPost Kurier") == "kurier"


class TestPickInpostServiceExplicitMap:
    def test_env_mapping_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INPOST_SERVICE_TITLE_MAP", "paczkomat=paczkomat;kurier=kurier")
        _reset_courier_maps_cache()
        assert _pick_inpost_service("Paczkomat 24/7") == "paczkomat"
        assert _pick_inpost_service("Kurier") == "kurier"

    def test_env_mapping_supports_new_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INPOST_SERVICE_TITLE_MAP", "pop=paczkomat")
        _reset_courier_maps_cache()
        assert _pick_inpost_service("POP odbiór") == "paczkomat"

    def test_falls_back_to_heuristic_on_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INPOST_SERVICE_TITLE_MAP", "pop=paczkomat")
        _reset_courier_maps_cache()
        # no keyword matches → heuristic returns 'kurier'
        assert _pick_inpost_service("InPost Standard") == "kurier"
        assert _pick_inpost_service("Paczkomat 24/7") == "paczkomat"


# ── _pick_apaczka_service ────────────────────────────────────────────────────


class TestPickApaczkaService:
    def test_no_env_configured_returns_none(self) -> None:
        assert _pick_apaczka_service("Apaczka DPD") is None

    def test_env_mapping_match_returns_service_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21;orlen paczka=53")
        _reset_courier_maps_cache()
        assert _pick_apaczka_service("Apaczka DPD") == "21"

    def test_env_mapping_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "orlen paczka=53")
        _reset_courier_maps_cache()
        assert _pick_apaczka_service("ORLEN PACZKA - punkt odbioru") == "53"

    def test_no_match_in_configured_map_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
        _reset_courier_maps_cache()
        assert _pick_apaczka_service("UPS Express") is None

    def test_json_env_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", '{"dpd": "21", "ups": "1"}')
        _reset_courier_maps_cache()
        assert _pick_apaczka_service("UPS Standard") == "1"

    def test_no_substring_heuristic_fallback(self) -> None:
        """Unlike _pick_courier/_pick_inpost_service, there is no heuristic here —
        Apaczka title strings aren't predictable substrings like inpost/paczkomat."""
        assert _pick_apaczka_service("Kurier ekspresowy XYZ") is None

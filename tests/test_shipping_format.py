"""Tests for zdrovena.common.shipping_format helpers."""

from zdrovena.common.shipping_format import (
    extract_locker_id_from_title,
    normalize_pl_phone,
    parse_pl_address,
)


class TestNormalizePlPhone:
    """Tests for normalize_pl_phone."""

    def test_nine_digits(self):
        assert normalize_pl_phone("600100200") == "+48600100200"

    def test_nine_digits_with_spaces(self):
        assert normalize_pl_phone("600 100 200") == "+48600100200"

    def test_nine_digits_with_hyphens(self):
        assert normalize_pl_phone("600-100-200") == "+48600100200"

    def test_eleven_digits_starting_48(self):
        assert normalize_pl_phone("48600100200") == "+48600100200"

    def test_eleven_digits_with_plus_and_spaces(self):
        assert normalize_pl_phone("+48 600-100-200") == "+48600100200"

    def test_leading_zero_gets_stripped(self):
        # "048600100200" is 12 digits, doesn't match 9-digit or 11-digit patterns
        assert normalize_pl_phone("048600100200") == "048600100200"  # returns original

    def test_empty_string_returns_none(self):
        assert normalize_pl_phone("") is None

    def test_none_returns_none(self):
        assert normalize_pl_phone(None) is None

    def test_unparseable_returns_original(self):
        assert normalize_pl_phone("12345") == "12345"

    def test_with_parentheses_and_spaces(self):
        assert normalize_pl_phone("(600) 100-200") == "+48600100200"


class TestParsePlAddress:
    """Tests for parse_pl_address."""

    def test_simple_street_number(self):
        street, building = parse_pl_address("Kwiatowa 1")
        assert street == "Kwiatowa"
        assert building == "1"

    def test_street_with_apartment(self):
        street, building = parse_pl_address("Marszałkowska 12/3")
        assert street == "Marszałkowska"
        assert building == "12/3"

    def test_street_with_apt_suffix(self):
        street, building = parse_pl_address("Aleja Niepodległości 100A m. 5")
        # Regex captures up to the letter suffix, rest is ignored
        assert street == "Aleja Niepodległości"
        assert building == "100A"

    def test_multi_word_street(self):
        street, building = parse_pl_address("Aleja Jana Sobieskiego 42")
        assert street == "Aleja Jana Sobieskiego"
        assert building == "42"

    def test_empty_string_returns_defaults(self):
        street, building = parse_pl_address("")
        assert street == ""
        assert building == "1"

    def test_unparseable_returns_original_with_default(self):
        street, building = parse_pl_address("Just Street")
        assert street == "Just Street"
        assert building == "1"

    def test_street_with_suffix_A(self):
        street, building = parse_pl_address("Testowa 10A")
        assert street == "Testowa"
        assert building == "10A"

    def test_street_with_slash_apartment(self):
        street, building = parse_pl_address("Piotrkowska 105/12")
        assert street == "Piotrkowska"
        assert building == "105/12"


class TestExtractLockerIdFromTitle:
    """Tests for extract_locker_id_from_title."""

    def test_extract_locker_id_from_inpost_title(self):
        title = "InPost • Paczkomat 24/7 • 2.51 km • RUH02M"
        result = extract_locker_id_from_title(title)
        assert result == "RUH02M"

    def test_extract_locker_id_from_dpd_title(self):
        title = "DPD • DPD Pickup- \"Lidl\" • 0.19 km • PL5A362"
        result = extract_locker_id_from_title(title)
        assert result == "PL5A362"

    def test_extract_locker_id_from_poczta_title(self):
        title = "Poczta Polska • Sklep Żabka • 0.16 km • 367682"
        result = extract_locker_id_from_title(title)
        assert result == "367682"

    def test_no_locker_id_in_kurier_title(self):
        title = "Kurier - dostawa pod drzwi"
        result = extract_locker_id_from_title(title)
        assert result == ""

    def test_invalid_locker_id_format(self):
        title = "Something • Something • x"  # 'x' alone doesn't match pattern
        result = extract_locker_id_from_title(title)
        assert result == ""

    def test_empty_string(self):
        result = extract_locker_id_from_title("")
        assert result == ""

    def test_no_bullet_separator(self):
        result = extract_locker_id_from_title("No separator here RUH02M")
        assert result == ""

    def test_locker_id_with_leading_trailing_spaces(self):
        title = "Inpost • Paczkomat • km • RUH02M "  # extra space
        result = extract_locker_id_from_title(title)
        assert result == "RUH02M"

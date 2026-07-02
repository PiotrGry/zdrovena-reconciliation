# Audyt techniczny — Fakturownia

**Zakres**: `zdrovena/common/fakturownia.py` (261 linii, klient REST).

**Metodologia**: kontraktowy audyt względem [dokumentacji Fakturownia API](https://app.fakturownia.pl/api) + [KSeF FA(3) field mapping](https://pomoc.fakturownia.pl/pola-przekazywane-z-programu-fakturownia-do-ksef-zgodnie-ze-schema-fa-3) + `ruff strict` + `pyright`.

**Wynik pyright**: ✅ 0 errors.
**Wynik ruff strict**: 🟡 14 znalezisk (głównie kosmetyczne: magic values HTTP 200/300/500/600, EM101/EM102/TRY003 dla stringów w exceptions).

**Ogólna ocena**: **Bardzo czysty klient**. Kontrakt endpointów zgodny z publiczną dokumentacją. Główne ryzyko: **`settlement_positions` field nie jest publicznie udokumentowane** (widoczne tylko w KSeF FA(3) mapping, bez schematu wierszy).

---

## 1. Kontraktowe niezgodności z API Fakturownia

**Zero bugów blokujących.** Endpointy, metody i parametry zgodne z dokumentacją.

### 1.1 Ryzyko krytyczne: `settlement_positions` — undocumented

| Aspekt                        | Nasz kod                                                     | Dokumentacja                                                   | Ryzyko |
| ----------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------- | ------ |
| Nazwa pola                    | `"settlement_positions"` (`fakturownia.py:203,231`)          | Wymienione w KSeF FA(3) mapping (Rozliczenie → obciążenia/odliczenia). **Brak szczegółu struktury wiersza**. | 🟡 medium |
| `kind` values                 | `"charge"`, `"deduction"` (`fakturownia.py:47-48`)          | **Brak publicznej dokumentacji tych wartości**.                | 🟥 high   |
| Format kwoty                  | String `"0.50"` (via `_normalize_amount_pln`)               | **Brak publicznej dokumentacji**.                             | 🟡 medium |
| Klucz opisu                   | `"description"`                                              | **Brak publicznej dokumentacji**.                             | 🟡 medium |
| PUT semantics                 | Przekazujemy pełną listę (z istniejącymi rowami + nowym)    | Nieudokumentowana, potwierdzona empirycznie                    | 🟡 medium |
| `id` zachowanie istniejących  | Kod NIE kopiuje `id` z istniejących wierszy (`fakturownia.py:222`) — używa `list(existing_rows)` (referencje, nie kopie!) | Rails PUT semantics zwykle wymagają `id` na kept rows          | 🟡 medium |

**Bug #F2-1** (🟡 medium): `fakturownia.py:222` — `merged: list[dict[str, Any]] = list(existing_rows)` tworzy **shallow copy** listy, ale dict-y w środku są **shared reference**. Jeśli między `get_invoice` a `update_invoice` inny worker zmienił `settlement_positions` w bazie, to nasz PUT nadpisze ich zmiany (last-write-wins). Fix: deep copy + weryfikacja czy istniejące `id` są kompletne.

**Bug #F2-2** (🟡 medium): docstring komentarza w `fakturownia.py:221` mówi *"Preserve existing rows verbatim (Rails PUT semantics require `id` on kept rows)"* — ale kod NIE zapewnia obecności `id`. Fix: filter/assert że każdy row z `existing_rows` ma `id` przed dodaniem do merged list.

**Rekomendacja**: napisać kontrakt-test integracyjny (opt-in, przeciwko sandboxowi Fakturownia) który weryfikuje semantykę PUT z `settlement_positions`. Alternatywnie — zamówić potwierdzenie od Fakturownia support (email: pomoc@fakturownia.pl) że `charge`/`deduction` są supported values dla `kind` w API i że schema jest stabilna.

### 1.2 Uzupełnienia dobrej praktyki

- `_request` (`fakturownia.py:89-113`) **poprawnie** obsługuje `requests.Timeout` → `CourierTimeoutError`, `requests.ConnectionError` → `CourierConnectionError`. 👍
- `_parse_response` (`fakturownia.py:115-136`) **poprawnie** mapuje status codes na hierarchię wyjątków:
  - `401/403` → `FakturowniaAuthError` ✅
  - `5xx` → `FakturowniaServerError` ✅
  - `4xx (inne)` → `FakturowniaBusinessError` ✅
  - `2xx` → JSON body ✅

- `add_settlement_position` (`fakturownia.py:174-231`) **poprawnie** implementuje idempotency guard (linie 210-219): sprawdza duplikat description po `casefold()` na `existing_rows` przed PUT. 👍

- `has_settlement_with_description` (`fakturownia.py:233-245`) — statyczna helper dla wołającego (patcher), umożliwia early-skip bez wykonania GET. 👍

---

## 2. Endpointy — inwentaryzacja

| #   | Method | Path                            | Idempotent | Retry safe | Użytkowane?                          |
| --- | ------ | ------------------------------- | ---------- | ---------- | ------------------------------------ |
| 1   | GET    | `/invoices/{id}.json`           | Tak         | ✅         | ✅ (`get_invoice`)                    |
| 2   | GET    | `/invoices.json?...`            | Tak         | ✅         | ✅ (`list_invoices` — filtry: period, oid, number, include_positions) |
| 3   | PUT    | `/invoices/{id}.json`           | Tak (PUT semantics) | ✅  | ✅ (`update_invoice`)                 |
| 4   | Composite | `add_settlement_position` (GET→PUT) | Tak (double-check idempotency) | ✅ | ✅ (`fakturownia_patcher.py`) |

**Zero martwych endpointów.** Klient minimalistyczny, tylko to co używane.

**Brakuje**: `POST /invoices.json` (create) — używane w kodzie? Nie widziałem call site. **OK, celowo poza scope.**

---

## 3. Antypaterny — ruff strict (14 znalezisk)

### 3.1 `fakturownia.py`

| Reguła    | Ile | Linie                       | Severity  | Opis                                                                                        |
| --------- | --- | --------------------------- | --------- | ------------------------------------------------------------------------------------------- |
| PLR2004   | 5   | 118, 119, 133 (×2), 118    | 🟢 low    | Magic values (200, 300, 204, 500, 600). Fix: stałe HTTP_OK, HTTP_MULTIPLE_CHOICES itd. **Do wspólnego cross-cutting fixu.** |
| TRY003    | 4   | 79, 193, 197, 200           | 🟢 low    | Długie messages w `raise ValueError(...)`. Fix: własne exception classes ALBO whitelist reguły. |
| EM101/EM102 | 4 | 80, 193, 197, 200           | 🟢 low    | Powiązane z TRY003 — string literal directly in raise. Fix jak wyżej.                        |
| PYI041    | 1   | 64                          | 🟢 low    | `int | float` → `float` (int akceptowalny przez float). Trywialny fix.                       |

**Ocena**: wszystkie znaleziska są **kosmetyczne / stylowe**. Zero antypaternów bezpieczeństwa, zero antypaternów wydajności, zero blind-exception. To wzorcowy plik do naśladowania w reszcie kodu.

### 3.2 `fakturownia_patcher.py` (endpointy)

Zobacz Raport Allegro §3.3 (bo patcher jest wspólny — audytowany razem z Allegro). Kluczowe znaleziska:

- **BLE001** (3× — linie 100, 113, 132) — `except Exception`. Fix wspólny z pollerem.
- **TRY400** (3× — linie 101, 114, 133) — `.error` → `.exception`.
- **RUF002** (2× — linie 10, 77) — `×` MULTIPLICATION SIGN w docstringu → `x`.

---

## 4. Idempotency

### 4.1 Klient (`fakturownia.py`)

- `get_invoice`, `list_invoices`, `update_invoice` — GET/PUT semantics, natywnie idempotent. ✅
- `add_settlement_position` — **explicit double-check idempotency**:
  1. Wołający robi `has_settlement_with_description(current_invoice, desc)` (early skip).
  2. Klient wewnątrz `add_settlement_position` PONOWNIE robi GET + check (race protection).
  Wzorzec **prawidłowy**. 👍

### 4.2 Rekomendacja

Dodać w `FakturowniaClient.__init__` opcjonalny param `idempotency_key: str | None = None` — jeśli set, przekazywać jako header `Idempotency-Key` (Fakturownia obecnie nie honoruje, ale gotowość na przyszłość + audit trail w naszych logach).

---

## 5. Exception mapping — audyt

### 5.1 Klient (`fakturownia.py`) — WZORZEC POZYTYWNY

Klient **poprawnie i kompletnie** mapuje wszystkie 3 rodzaje błędów na typowane wyjątki z `shipping_exceptions.py`:

```
requests.Timeout        → CourierTimeoutError(courier="fakturownia")  # transient, retry
requests.ConnectionError → CourierConnectionError(courier=..., detail=...)  # transient
HTTP 401/403             → FakturowniaAuthError(detail=...)  # auth failure, alert
HTTP 5xx                 → FakturowniaServerError(status=...)  # transient
HTTP 4xx (inne)          → FakturowniaBusinessError(detail=..., action=...)  # bad request
HTTP 2xx                 → JSON body (Any)
```

**To wzorzec do skopiowania** do klientów InPost i Apaczka (które nie mają hierarchii).

### 5.2 Patcher (`fakturownia_patcher.py`) — problematyczny

- **BLE001** w 3 miejscach — patcher powinien łapać `FakturowniaAuthError`, `FakturowniaBusinessError`, `CourierServerError` osobno i mapować:
  - `FakturowniaAuthError` → alert + `raise`
  - `FakturowniaBusinessError` → log + `continue` (per invoice, nie blokuje batcha)
  - `CourierServerError` / `CourierTransientError` → log + retry counter, continue

Fix wspólny z Allegro (Blok C w Raporcie Allegro).

---

## 6. Logging

### 6.1 Klient — cichy

`fakturownia.py` używa `log = logging.getLogger(__name__)` i loguje TYLKO 1 rzecz: `log.info("add_settlement_position: invoice %s already has row %r — skipping PUT", ...)` (linia 214).

**Rekomendacja**: dodać INFO log na successful update_invoice (audit trail dla PIT/KSeF), WARNING na retry (`FakturowniaServerError`).

### 6.2 Patcher — do naprawy

3× `.error` z exc → 3× `.exception()` (Fix F11 w Raporcie Allegro).

**Rekomendacja dodatkowa**: `fakturownia_patcher.py:126-141` (patchowanie 1 faktury) — brakuje logu INFO na happy path. Powinno być: `log.info("Patched invoice %s (order %s): added kaucja %s PLN", invoice_number, order_id, kaucja_amount)`.

---

## 7. Lista fixów do finalnego PR (Fakturownia)

**Blok A — Undocumented API contract (medium):**

- [ ] **FA1**: Dodać integration test (opt-in, `@pytest.mark.integration`) który tworzy testową fakturę w sandboxie Fakturownia, PATCH-uje settlement_positions z `kind=charge`, waliduje że GET zwraca zapisany row. Docelowo run raz na tydzień w CI (nightly).
- [ ] **FA2**: `fakturownia.py:222` — deep copy `existing_rows` przed PATCH-em (zapobiega mutacji przez shared ref).
- [ ] **FA3**: `fakturownia.py:222` — assert że każdy row w `existing_rows` ma `id` (fail-fast jeśli Fakturownia zmieni schema).

**Blok B — Cosmetics (low, do cross-cutting):**

- [ ] **FA4**: `fakturownia.py:64` — `int | float` → `float`.
- [ ] **FA5**: `fakturownia.py:118,133` — magic HTTP status codes → stałe (razem z Allegro Fix F17).
- [ ] **FA6**: `fakturownia.py:79-80,193,197,200` — TRY003/EM101/EM102 → własne exception classes ALBO whitelist tych reguł w `pyproject.toml` (rekomenduję whitelist — `ValueError` z tekstem jest OK, to Pythonic).

**Blok C — Exception & logging (wspólne z Allegro):**

- [ ] Patcher: fixy F9, F11 z Raportu Allegro.
- [ ] Klient: dodać `log.info` na `update_invoice` (audit trail).

**Estymacja**: ~50 linii kodu, 1 integration test file (~80 linii). **W tym samym gigantycznym PR co Allegro.**

---

## Załączniki

**Źródła**:
- [Fakturownia API — Publiczna dokumentacja](https://app.fakturownia.pl/api)
- [Fakturownia KSeF FA(3) field mapping](https://pomoc.fakturownia.pl/pola-przekazywane-z-programu-fakturownia-do-ksef-zgodnie-ze-schema-fa-3) (2026-06-11)
- [Fakturownia GitHub API examples](https://github.com/fakturownia/API)

# Fakturownia API — pole `settlement_positions` (Rozliczenie / kaucja)

## Znalezione dowody (Task 7 recon)

### 1. Oficjalne źródło Fakturowni (2026-06-11)

Strona [pomoc.fakturownia.pl — "Pola przekazywane do KSeF FA(3)"](https://pomoc.fakturownia.pl/pola-przekazywane-z-programu-fakturownia-do-ksef-zgodnie-ze-schema-fa-3) zawiera tabelę mapowania:

| Pole KSeF | Zmienna w Fakturowni | Lokalizacja w programie | Opis |
|---|---|---|---|
| **Rozliczenie** | **`settlement_positions`** | Faktura → Rozliczenie | Lista obciążeń i odliczeń (np. odsetki, kompensata); generowane gdy lista jest niepusta |

To dokładnie węzeł widoczny w UI Fakturowni na screenach użytkownika (Rozliczenie / obciążenia i odliczenia).

### 2. Włączanie w UI

Ustawienia → Faktury → włącz **"Używaj rozliczenia KSeF"** ([pomoc.fakturownia.pl](https://pomoc.fakturownia.pl/wezel-rozliczenie-jak-dodac-obciazenia-i-odliczenia-na-fakturze-ksef)).

### 3. Struktura KSeF FA(3) `Rozliczenie`

Zgodnie z broszurą MF, węzeł `Rozliczenie` zawiera:
- `Obciazenia` (charge) → `Kwota` (decimal) + `Powod` (string)
- `Odliczenia` (deduction) → `Kwota` (decimal) + `Powod` (string)
- `SumaObciazen` (auto-liczone)
- `SumaOdliczen` (auto-liczone)

### 4. Wnioskowana struktura API (do potwierdzenia empirycznie)

Konwencja Rails w Fakturowni (`positions`, `correction_before_attributes`, itp.) sugeruje że `settlement_positions` to lista dictów. Prawdopodobny kształt:

```json
{
  "api_token": "TOKEN",
  "invoice": {
    "settlement_positions": [
      {
        "kind": "charge",              // "charge" (obciążenie) | "deduction" (odliczenie)
        "amount": "10.00",             // kwota PLN
        "description": "Kaucja za opakowania zwrotne"  // powód (KSeF `Powod`)
      }
    ]
  }
}
```

Alternatywne nazwy subfields do sprawdzenia empirycznie (kolejność prawdopodobieństwa):
1. `amount` + `description` + `kind`  ← najbardziej prawdopodobne (Rails default)
2. `kwota` + `powod` + `rodzaj`  ← mało prawdopodobne (Fakturownia używa EN kluczy w API)
3. `settlement_amount` + `settlement_description` + `settlement_kind`
4. `value` + `reason` + `type`

## Plan implementacji

### Faza A — REST client z `settlement_positions`

Utworzyć `zdrovena/common/fakturownia.py`:
- `FakturowniaClient(base_url, api_token)`
- `.get_invoice(invoice_id)` → GET `/invoices/{id}.json?api_token=…`
- `.update_invoice(invoice_id, patch)` → PUT `/invoices/{id}.json`
- `.add_settlement_position(invoice_id, *, kind, amount_pln, description)` → wysyła PUT z `settlement_positions` (append semantics)
- Error mapping: 401/403→AuthError, 4xx→BusinessError, 5xx→ServerError, timeouts→TimeoutError

### Faza B — Worker `fakturownia_patcher`

`zdrovena/api/routers/fakturownia_patcher.py`:
- `poll_allegro_invoices_once()`:
  1. Pobierz przez `AllegroClient.list_order_invoices` faktury dla ostatnich N zamówień z `source='allegro'`
  2. Dla każdej faktury: sciągnij plik z Fakturowni albo z `positions` w response
  3. Policz PET butelki: `count_pet_bottles(positions)` z `zdrovena.common.bottles`
  4. `kaucja_amount = KAUCJA_UNIT_PRICE_PLN * pet_count` (0.50 * N)
  5. Sprawdź idempotency — czy faktura ma już `settlement_positions` z description="Kaucja za opakowania zwrotne"
  6. Jeśli nie — PUT z `settlement_positions: [{"kind":"charge","amount":kaucja,"description":"Kaucja za opakowania zwrotne"}]`

### Faza C — Weryfikacja empiryczna (RUNTIME)

Ponieważ Fakturownia nie dokumentuje publicznie subfields `settlement_positions`, TDD-first pattern:
1. Napisać testy z założoną strukturą `{kind, amount, description}` (mock)
2. W środowisku sandbox/test uruchomić 1 real API call
3. Jeśli struktura się różni — zaktualizować adapter (jednolinijkowa zmiana, testy nie zależą od nazw subfields tylko od contract)

### Faza D — Env vars

```env
FAKTUROWNIA_BASE_URL=https://zdrovena.fakturownia.pl
FAKTUROWNIA_API_TOKEN=<sekret z Key Vault>
KAUCJA_UNIT_PRICE_PLN=0.50
KAUCJA_DESCRIPTION="Kaucja za opakowania zwrotne"
ALLEGRO_INVOICES_POLL_SECONDS=900
```

## Ryzyka i mitigacje

1. **Nieznane subfields** — mitigacja: layer adaptera `_build_settlement_payload()` osobno testowany; zmiana kluczy = 1 linia
2. **Fakturownia UI wymaga "Używaj rozliczenia KSeF"** — dokument w README, user musi włączyć raz w Ustawieniach
3. **Idempotency** — sprawdzać istniejące `settlement_positions` przed patchem po `description=KAUCJA_DESCRIPTION`
4. **PUT overwrite vs append** — Rails `_attributes` zazwyczaj REPLACE; testować `include existing + new` żeby zachować istniejące pozycje

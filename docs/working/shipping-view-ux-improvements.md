# ShippingView — UX improvements (working notes)

Status: **do zaimplementowania** — nie tykać do decyzji

---

## 1. Przycisk "Rozwiń" zamiast klikalnego nagłówka

Obecny stan: kliknięcie całego paska nagłówka row razvija/zwija accordion.
Żądana zmiana: tylko dedykowany przycisk "Rozwiń ▼ / Zwiń ▲" po prawej stronie row otwiera szczegóły. Cały wiersz nie powinien być klikalny.

Dotyczy: `DraftRow` w `frontend/src/views/ShippingView.jsx` — zmiana `onClick` z całego `<button className="accordion-header">` na dedykowany element.

---

## 2. Email i telefon klienta widoczne przed rozwinięciem

Obecny stan (zwinięty): `#1111 Katarzyna Nowak · InPost Paczkomat · data · status`
Żądany stan (zwinięty): `#1111 Katarzyna Nowak · email · tel · InPost Paczkomat · data · status`

Dane są dostępne w `draft.receiver.email` i `draft.receiver.phone`.
Uwaga: nagłówek jest już gęsty — rozważyć skrócenie lub drugą linię.

---

## 3. Email klienta brakuje po rozwinięciu

W detail-grid (accordion body) jest `TELEFON` ale brak `EMAIL`.
Dodać pole EMAIL w detail-grid obok TELEFON.

Dane: `draft.receiver.email`

---

## 4. Opis paczek — wyświetlać typ kartonu, nie tylko liczbę

Obecny stan: `1 paczka` / `2 paczki`
Żądany stan: `1 paczka` z podpisem np. `1×3-pak` lub `1×3-pak + 1×szkło-2pak`

`packages_breakdown` jest już zapisywany w drafcie jako lista `[{type, qty}]`.
Frontend korzysta z `breakdownLabel()` — musi być widoczny w zwiniętym widoku, nie tylko po rozwinięciu.

### Typy kartonów (z docs/superpowers/specs + nowy)

| Typ | Zgrzewki | Materiał |
|---|---|---|
| `3-pak` | 3 | plastik |
| `2-pak` | 2 | plastik |
| `1-pak` | 1 | plastik |
| `pół-pak` | 0.5 | plastik |
| `szkło` | 1 | szkło, 1 pudełko/zgrzewkę |
| `szkło-2pak` | 2 | **NOWY** — szkło, karton na 2 zgrzewki |

**Nowy typ `szkło-2pak`:** zgłoszony przez właściciela 2026-06-24. Wymaga:
- Aktualizacji `_calc_packages()` w `zdrovena/api/routers/webhooks.py`
- Aktualizacji spec `docs/superpowers/specs/2026-05-15-shipping-draft-automation-design.md`
- Nowych testów w `tests/test_shipping_webhook.py`

Algorytm szkło (proponowany po zmianie):
```
glass remaining >= 2: use szkło-2pak, remaining -= 2
glass remaining == 1: use szkło (1-pak)
```

---

## Pliki do zmiany

- `frontend/src/views/ShippingView.jsx` — punkty 1, 2, 3, 4 (display)
- `zdrovena/api/routers/webhooks.py` — punkt 4 (nowy typ szkło-2pak)
- `docs/superpowers/specs/2026-05-15-shipping-draft-automation-design.md` — zaktualizować box types
- `tests/test_shipping_webhook.py` — nowe testy dla szkło-2pak

<!-- # PLAN

## Backlog (checklist)
- [ ] Etap 1: ...
- [ ] Etap 2: ...
- [ ] Etap 3: ...

## Ryzyka / zależności
- ... -->

## Etap 1 – ujednolicenie Period dla `close`

- [ ] Zmienić CLI na: `zdrovena close --period YYYY-MM` (docelowy interfejs)
- [ ] (Opcjonalnie) zachować kompatybilność: `zdrovena close YYYY-MM` jako alias w okresie przejściowym
- [ ] Forwardować period z `close` do audytu uruchamianego w pipeline
- [ ] Testy parsowania period + test “close → audit (ten sam period)”

## Etap 2 – Audit jako bramka przed ZIP/Email

- [ ] Zdefiniować AuditResult + severity INFO/WARN/FAIL
- [ ] Audit zwraca dane strukturalne (nie tylko print)
- [ ] FAIL blokuje krok ZIP + Email
- [ ] WARN/INFO nie blokują, ale trafiają do raportu końcowego

## Etap 3 – ZIP jako artefakt “produkcyjny”

- [ ] Potwierdzić strukturę folderu miesiąca jako kontrakt ZIP:
  - sprzedaz/
  - koszty/
  - JPK_FA.xml, JPK_V7M.xml, Wykaz_sprzedazy_VAT.pdf w root
- [ ] Dodać export AuditResult do pliku (np. audit_result.json) do root month_dir (żeby trafił do ZIP)
- [ ] Test: ZIP zawiera podfoldery + pliki root + audit_result.json, a nie zawiera .state.json/.file_hashes.json

## Etap 4 – Re-open (future-ready)

- [ ] ADR: jak wersjonujemy zamknięcia (nadpisywanie vs wersje)
- [ ] (future) model DB: months + audit_runs + artifacts
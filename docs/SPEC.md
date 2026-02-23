<!-- # SPEC

## Cel
(Opisz w 1–2 zdaniach po co jest ten projekt.)

## Zakres
- In scope:
- Out of scope:

## Interfejsy
- CLI:
- Integracje:

## Wymagania niefunkcjonalne
- Testy
- Logowanie
- Bezpieczeństwo

## Definition of Done
- [ ] Testy przechodzą
- [ ] Zaktualizowany RUNBOOK
- [ ] Zgodność z ustalonymi zasadami -->


# SPEC – Zdrovena Reconciliation

## 1. Cel systemu

Celem aplikacji jest przygotowanie miesięcznej paczki rozliczeniowej:
- zebranie dokumentów za zamykany miesiąc,
- uruchomienie audytu/walidacji danych,
- wygenerowanie paczki ZIP,
- wysłanie ZIP mailem do księgowej.

W przyszłości: WebUI + baza danych (historia zamknięć, wyniki audytu, artefakty).

---

## 2. Zakres i moduły

### 2.1 In Scope (v1)
- CLI jako główny interfejs
- Month Closing (pipeline zamknięcia miesiąca)
- Audit (walidacja danych – sekcje audytu)
- Generowanie ZIP z artefaktami miesiąca
- Wysyłka e-mail do księgowej
- Integracje wykorzystywane przez pipeline (wg istniejącego kodu)

### 2.2 Out of Scope (na teraz)
- WebUI
- Trwały zapis wyników do DB (docelowo będzie)
- Kolejki / job scheduler (docelowo może być)

### 2.3 Moduły (obecna architektura)
- `zdrovena/cli.py` – rejestracja komend CLI (argparse, subcommands)
- `zdrovena/month_closing/*` – pipeline zamknięcia miesiąca (orchestrator + kroki)
- `zdrovena/audit/*` – audyt i raportowanie
- `zdrovena/common/*` – wspólne elementy (retry, config, helpery)
- `zip_service` (w module month_closing) – tworzenie archiwum ZIP

Granice modułów: zgodnie z ADR 0001.

---

## 3. Interfejs CLI (stan na dziś + target)

CLI działa w modelu subcommand.

### 3.1 Komendy audytu (wspólne argumenty: --year/--month/--day)
- `zdrovena audit -y YYYY [-m MM]`  
  uruchamia audyt za rok lub miesiąc
- `zdrovena list -y YYYY -m MM [-d DD]`  
  listuje faktury dla okresu
- `zdrovena export -y YYYY`  
  eksport CSV
- `zdrovena summary -y YYYY`  
  podsumowanie WZ vs FV
- `zdrovena report -y YYYY -m MM`  
  generuje raport PDF
- `zdrovena products`  
  lista produktów

### 3.2 Month closing (zamknięcie miesiąca)
Stan w repo (obecnie):
- `zdrovena close YYYY-MM [--dry-run] [--zip] [--send] [--reset] [-v] [--non-interactive] [--ignore-warnings]`

Target (docelowo – preferowany interfejs):
- `zdrovena close --period YYYY-MM ...`

Uwaga:
- Docelowo chcemy jeden spójny sposób przekazywania okresu (Period) dla komend “miesięcznych”.
- Dopuszczamy okres przejściowy (kompatybilność wstecz) jeśli to ułatwi migrację.

---

## 4. Kluczowa koncepcja: Period

System operuje na pojęciu **Period = (rok, miesiąc)**.

- Month closing zawsze działa na jednym okresie (miesiącu).
- Ten sam okres ma być przekazywany do audytu uruchamianego w ramach close (forwarding).
- Audit uruchamiany samodzielnie z CLI może nadal wspierać tryb “rok” (month=None).

**Single Source of Truth:** period ustala wywołanie `zdrovena close ...` i jest przekazywany dalej do wewnętrznych kroków.

---

## 5. Flow: Month Closing → Audit → ZIP → Email

Pipeline dla zamykanego period:

1. Collect / Normalize danych i dokumentów do folderu miesiąca
2. Audit (walidacja) dla tego samego period
3. Decision:
   - FAIL blokuje generowanie ZIP i wysyłkę
   - WARN dopuszcza ZIP (i zapis ostrzeżeń w raporcie)
   - INFO tylko informacyjnie
4. ZIP: spakowanie folderu miesiąca (sprzedaz/, koszty/, raporty w root)
5. Email: wysyłka ZIP do księgowej (tylko jeśli nie ma FAIL)

Tryby wykonania (wg CLI):
- full pipeline (domyślnie)
- zip-only
- send-only
- zip+send
- dry-run (symulacja)
- reset (wymusza powtórzenie kroków)

---

## 6. Monthly Validation Rules (v1)

Źródło prawdy: moduł `zdrovena.audit.sections` (sekcje audytu).

Sekcje (skrót):
- Recount (FV vs WZ totals per month)
- Type-level match (plastik/szkło FV vs WZ)
- Orphan WZ (WZ bez faktury)
- Invoices without WZ (FV z butelkami bez WZ)
- Date comparison (sell_date FV vs issue_date WZ – cross-month)
- Cross-month sell/issue w samej FV
- Numbering continuity (luki/duplikaty numeracji)
- Stock balance (PZ/WZ dla butelek)
- Anomalies (>72 butelek, wiele FV do jednego WZ)

---

## 7. Kontrakt audytu (pod WebUI + DB)

Audit zwraca strukturę danych:

AuditResult:
- status: INFO | WARN | FAIL (globalny wynik)
- issues: lista {code, severity, message, metadata}
- summary: liczniki per sekcja / severity

Docelowo:
- serializowalne do JSON
- widoczne w WebUI
- zapisywalne w DB

---

## 8. ZIP (kontrakt artefaktu)

ZIP jest tworzony z folderu miesiąca:

BASE_DIR/<rok>/<miesiąc_pl>/

ZIP zawiera rekurencyjnie wszystkie pliki i podfoldery w folderze miesiąca,
z wyłączeniem:
- samego ZIP
- `.state.json`
- `.file_hashes.json`
- `.DS_Store`

W ramach month closing w folderze miesiąca występują m.in.:
- `sprzedaz/` (faktury sprzedaży)
- `koszty/` (faktury kosztowe)
- raporty w root folderu miesiąca (np. JPK, VAT)
- (docelowo) `audit_result.json`

---

## 9. Reversible month close (re-open)

Zamknięcie miesiąca jest odwracalne (re-open).
Ponowne uruchomienie close dla tego samego period:
- ponownie uruchamia audit
- generuje nowy ZIP
- (future) zapisuje nową wersję w DB

Szczegóły wersjonowania zostaną określone po wprowadzeniu DB.

---

## 10. Wymagania niefunkcjonalne

- Testy jednostkowe dla logiki krytycznej
- Logowanie przebiegu pipeline (INFO) + debug (verbose)
- Brak sekretów w repo (sekrety w zewnętrznym storage)
- Jedno polecenie quality gate: `./scripts/check.sh`

---

## 11. Definition of Done

Funkcjonalność close jest ukończona gdy:
- close uruchamia audit dla tego samego period (forwarding)
- FAIL blokuje ZIP + Email
- ZIP spełnia kontrakt i nie pakuje plików stanu
- Testy przechodzą
- `./scripts/check.sh` przechodzi
- RUNBOOK opisuje uruchomienie close i interpretację wyniku
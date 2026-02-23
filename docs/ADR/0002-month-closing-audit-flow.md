# ADR 0003 – Month Closing → Audit → ZIP → Email

## Date
2026-01-23 

## Authors
Piotr Gryzło


## Status
Accepted

## Context

System Zdrovena służy do przygotowania miesięcznej paczki rozliczeniowej
(ZIP) i wysłania jej mailem do księgowej.

Istnieje moduł audit, który weryfikuje poprawność danych finansowych
i operacyjnych (FV, WZ, PZ, numeracje, anomalie, bilans).

System wprowadza pojęcie "period" (rok + miesiąc),
np. 2026-01.

Decyzja dotyczy:
- relacji między month_closing a audit
- momentu uruchamiania audytu
- wpływu wyniku audytu na zamknięcie miesiąca
- przekazywania argumentów daty
- odwracalności zamknięcia

---

## Decision

1. Single Source of Truth – Period

Month closing przyjmuje parametr:

    --period YYYY-MM

Ten period jest:
- jedynym źródłem prawdy dla zakresu czasowego
- przekazywany bez modyfikacji do audit
- używany do generowania raportów i ZIP
- (w przyszłości) zapisywany w DB

Audit nie ustala zakresu samodzielnie.

---

2. Flow wykonania

Pipeline month closing:

1. Collect
2. Normalize
3. Audit (dla tego samego period)
4. Decision
5. ZIP
6. Email

---

3. Kontrakt wyniku audytu

Audit zwraca strukturę danych:

AuditResult:
- status: INFO | WARN | FAIL
- issues: lista {code, severity, message, metadata}
- summary: agregaty

Severity:
- INFO – informacyjne
- WARN – ostrzeżenie
- FAIL – błąd krytyczny

---

4. Wpływ audytu na zamknięcie

- FAIL → blokuje generowanie ZIP i wysyłkę maila
- WARN → pozwala kontynuować
- INFO → pozwala kontynuować

System nie wysyła paczki przy FAIL.

---

5. Struktura ZIP (kontrakt)

ZIP tworzony z folderu miesiąca:

    BASE_DIR/<rok>/<miesiąc_pl>/

ZIP zawiera:
- sprzedaz/
- koszty/
- pliki raportowe w root (np. JPK_FA.xml)
- audit_result.json (future)

ZIP wyklucza:
- samego siebie
- pliki stanu (.state.json, .file_hashes.json)

---

6. Re-open (odwracalność)

Zamknięcie miesiąca jest odwracalne.

Ponowne uruchomienie month closing dla tego samego period:
- ponownie uruchamia audit
- generuje nowy ZIP
- (future) zapisuje nową wersję w DB

Szczegóły wersjonowania zostaną określone w osobnym ADR (po wprowadzeniu bazy danych).

---

## Consequences

### Pozytywne
- Spójny model danych
- Deterministyczne uruchamianie audytu
- Brak duplikacji logiki dat
- Gotowość pod WebUI i DB
- Wyraźna bramka jakości przed wysyłką do księgowej

### Negatywne
- Month closing zależy od audytu (większa odpowiedzialność)
- Konieczność strukturalnego wyniku audytu (refactor)

---

## Alternatives Considered

1. Audit ustala zakres samodzielnie
   Odrzucone – prowadzi do niespójności.

2. Audit jako opcjonalny krok
   Odrzucone – zwiększa ryzyko wysłania błędnych danych do księgowej.

3. FAIL jako ostrzeżenie
   Odrzucone – ryzyko operacyjne.
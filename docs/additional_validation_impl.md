# Plan implementacji dodatkowych walidacji księgowych

## Cel

Celem warstwy walidacyjnej jest wykrywanie błędów i anomalii księgowych **przed eksportem, zamknięciem miesiąca lub generacją JPK**, tak aby ograniczyć:
- błędy formalne,
- błędy rachunkowe,
- niespójności logiczno-księgowe,
- ryzyka audytowe.

Walidacje powinny działać jako **silnik reguł** zwracający wynik w ujednoliconym formacie, np.:

- `PASS`
- `WARN`
- `FAIL`

Każdy wynik powinien zawierać:
- `rule_id`
- `name`
- `severity`
- `scope`
- `message`
- `suggested_action`
- `document_ids`
- `details`

---

## Założenia architektoniczne

### Zakres encji
Walidacje mogą dotyczyć:
- faktur sprzedażowych,
- faktur kosztowych,
- korekt,
- dokumentów magazynowych (jeśli występują),
- płatności,
- kontrahentów,
- rekordów JPK,
- danych historycznych do analizy anomalii.

### Poziomy walidacji
Walidacje powinny działać na kilku poziomach:
1. **pojedynczy dokument**
2. **powiązane dokumenty**
3. **cały okres / miesiąc**
4. **analiza historyczna / anomalia**

### Priorytety wdrożeniowe
- **P1** — must have, wdrożenie w pierwszej kolejności
- **P2** — ważne walidacje logiczne i operacyjne
- **P3** — warstwa anomalii i scoringu ryzyka

---

# P1 — walidacje krytyczne / must have

## 1. Walidacje formalne dokumentu

### P1.1. Kompletność pól obowiązkowych
**Cel:** wykrycie dokumentów niekompletnych.

**Zakres sprawdzeń:**
- numer dokumentu,
- data wystawienia,
- data sprzedaży / operacji,
- dane kontrahenta,
- NIP jeśli wymagany,
- waluta,
- pozycje dokumentu,
- oznaczenie stawki VAT lub NP/ZW/0%.

**Warunek błędu:**
- brak dowolnego pola wymaganego dla danego typu dokumentu.

**Severity:** `FAIL`

---

### P1.2. Poprawność formatów
**Cel:** wykrycie błędnych danych technicznych.

**Zakres sprawdzeń:**
- format NIP,
- format dat,
- poprawność kodu waluty,
- liczby dodatnie / ujemne w dozwolonych polach,
- format numeru dokumentu.

**Severity:** `FAIL` lub `WARN` zależnie od pola

---

### P1.3. Duplikaty dokumentów
**Cel:** wykrycie zdublowanych faktur lub kosztów.

**Heurystyki:**
- ten sam numer dokumentu + ten sam kontrahent,
- ten sam kontrahent + ta sama kwota + ta sama data,
- podobny numer + podobna kwota + podobna treść.

**Wyniki:**
- twardy duplikat → `FAIL`
- miękkie podejrzenie duplikatu → `WARN`

---

## 2. Walidacje rachunkowe

### P1.4. Spójność netto + VAT = brutto
**Cel:** wykrycie błędów rachunkowych.

**Zakres:**
- poziom pozycji,
- poziom nagłówka dokumentu.

**Reguła:**
`abs((net + vat) - gross) > tolerance`

**Severity:** `FAIL`

---

### P1.5. Suma pozycji vs nagłówek
**Cel:** weryfikacja zgodności pozycji z podsumowaniem dokumentu.

**Sprawdzane pola:**
- suma netto pozycji,
- suma VAT pozycji,
- suma brutto pozycji.

**Porównanie z:**
- netto dokumentu,
- VAT dokumentu,
- brutto dokumentu.

**Severity:** `FAIL`

---

### P1.6. Poprawność wyliczenia VAT
**Cel:** sprawdzenie, czy VAT wynika prawidłowo ze stawki i podstawy.

**Zakres:**
- dla każdej pozycji,
- dla sumy dokumentu.

**Uwagi:**
- uwzględnić tolerancję zaokrągleń,
- rozróżnić błąd twardy od dopuszczalnej różnicy groszowej.

**Severity:** `FAIL` lub `WARN`

---

### P1.7. Kontrola wartości zerowych i ujemnych
**Cel:** wykrycie podejrzanych wartości.

**Flagować:**
- dokument o wartości 0,
- pozycja z ilością 0,
- cena jednostkowa 0,
- ujemna kwota bez oznaczenia korekty.

**Severity:** zwykle `WARN`, czasem `FAIL`

---

## 3. Walidacje dat i okresów

### P1.8. Relacja data wystawienia vs data sprzedaży
**Cel:** wykrycie nielogicznych relacji między datami.

**Przykłady:**
- data sprzedaży znacznie późniejsza niż wystawienia,
- data dokumentu poza dozwolonym okresem raportowania.

**Severity:** `WARN` lub `FAIL`

---

### P1.9. Cross-month boundary checks
**Cel:** wykrycie problemów na granicach miesięcy.

**Zakres:**
- sprzedaż w jednym miesiącu, wystawienie w innym,
- koszt dotyczący innego okresu niż data wpływu / wystawienia,
- dokument historyczny dodany po zamknięciu miesiąca.

**Severity:** `WARN`

---

### P1.10. Zmiana dokumentu po zamknięciu miesiąca
**Cel:** audit i ochrona integralności okresu.

**Wykrywać:**
- nowy dokument w zamkniętym miesiącu,
- edycję dokumentu po zamknięciu,
- usunięcie dokumentu wpływającego na okres.

**Severity:** `FAIL` lub `WARN`
**Dodatkowo:** obowiązkowy audit trail

---

## 4. Walidacje korekt i dokumentów źródłowych

### P1.11. Korekta ↔ dokument źródłowy
**Cel:** upewnienie się, że korekta ma poprawne źródło.

**Sprawdzane elementy:**
- istnienie dokumentu pierwotnego,
- zgodność kontrahenta,
- zgodność waluty,
- sensowność kierunku korekty,
- wartość korekty nieprzekraczająca logicznych granic.

**Severity:** `FAIL`

---

## 5. Walidacje gotowości do JPK

### P1.12. Spójność danych źródłowych z rekordem JPK
**Cel:** wykrycie rozjazdu między dokumentem a eksportem.

**Porównywać:**
- daty,
- kontrahenta,
- kwoty netto/VAT/brutto,
- typ dokumentu,
- oznaczenia VAT.

**Severity:** `FAIL`

---

### P1.13. Brak wymaganych pól do JPK
**Cel:** wykrycie braków uniemożliwiających poprawny eksport.

**Przykłady:**
- brak typu dokumentu,
- brak wymaganego oznaczenia,
- brak przypisania do odpowiedniego pola JPK.

**Severity:** `FAIL`

---

### P1.14. Reconciliation sum kontrolnych przed eksportem
**Cel:** kontrola zgodności raportu z systemem źródłowym.

**Porównania:**
- suma sprzedaży w systemie vs suma sprzedaży w JPK,
- suma VAT w systemie vs suma VAT w JPK,
- liczba dokumentów źródłowych vs liczba rekordów.

**Severity:** `FAIL`

---

# P2 — walidacje ważne logicznie i operacyjnie

## 6. Ciągłość numeracji

### P2.1. Ciągłość numeracji dokumentów sprzedażowych
**Cel:** wykrycie luk, duplikatów i nietypowych przeskoków.

**Zakres:**
- osobno per seria,
- osobno per typ dokumentu.

**Severity:** `WARN`

---

### P2.2. Ciągłość dokumentów powiązanych
**Cel:** wykrycie brakujących ogniw procesu.

**Dotyczy:**
- FV ↔ WZ,
- korekta ↔ dokument pierwotny,
- inne relacje, jeśli obecne w systemie.

**Severity:** `WARN`

---

## 7. Walidacje powiązań między dokumentami

### P2.3. Faktura sprzedażowa ↔ dokument magazynowy
**Cel:** wykrycie niespójności ilościowej i towarowej.

**Porównywać:**
- SKU,
- ilości,
- daty,
- obecność dokumentu powiązanego.

**Severity:** `WARN` lub `FAIL`

---

### P2.4. Koszt ↔ płatność
**Cel:** wykrycie nierozliczonych lub błędnie powiązanych kosztów.

**Sprawdzać:**
- zgodność kwoty,
- zgodność waluty,
- zgodność kontrahenta,
- częściowe płatności,
- nadpłaty i niedopłaty.

**Severity:** `WARN`

---

### P2.5. Refund / zwrot ↔ korekta
**Cel:** wykrycie niespójności pomiędzy zwrotem środków a korektą dokumentu.

**Przykłady:**
- refund bez korekty,
- korekta bez refundu tam, gdzie refund powinien istnieć,
- wielokrotna refundacja do jednego dokumentu.

**Severity:** `WARN` lub `FAIL`

---

## 8. Walidacje kontrahenta

### P2.6. Spójność danych kontrahenta
**Cel:** wykrycie rozjechanych kartotek.

**Wykrywać:**
- ten sam NIP, różne nazwy,
- ta sama nazwa, różne NIP-y,
- nietypowe zmiany kraju / waluty / adresu.

**Severity:** `WARN`

---

### P2.7. Kraj kontrahenta vs typ transakcji
**Cel:** wykrycie błędnej klasyfikacji VAT.

**Przykłady:**
- transakcja zagraniczna potraktowana jak krajowa,
- niespójna stawka VAT względem kraju kontrahenta,
- błędna klasyfikacja eksportu / WDT / importu usług.

**Severity:** `WARN` lub `FAIL`

---

### P2.8. B2B vs B2C
**Cel:** wykrycie niespójności danych zakupowych i księgowych.

**Sprawdzać:**
- brak NIP przy transakcji wyglądającej na B2B,
- obecność NIP, ale klasyfikacja jak B2C,
- rozjazd checkout vs dokument księgowy.

**Severity:** `WARN`

---

## 9. Walidacje kosztów cyklicznych

### P2.9. Brak dokumentu cyklicznego za okres
**Cel:** wykrycie brakujących kosztów abonamentowych i operacyjnych.

**Przykłady dostawców:**
- Shopify,
- Allegro,
- PayU,
- InPost,
- Zoho,
- inni powtarzalni usługodawcy.

**Severity:** `WARN`

---

### P2.10. Dubel dokumentu cyklicznego
**Cel:** wykrycie wielokrotnego ujęcia tego samego kosztu.

**Severity:** `FAIL` lub `WARN`

---

### P2.11. Koszt poza typowym okresem
**Cel:** wykrycie przesunięć i opóźnień.

**Przykłady:**
- dokument za dany miesiąc zaksięgowany niestandardowo,
- koszt z nietypową datą wpływu,
- ponowne ujęcie tego samego okresu rozliczeniowego.

**Severity:** `WARN`

---

## 10. Walidacje pozycji dokumentu

### P2.12. Niezgodność SKU / stawki / jednostki
**Cel:** wykrycie błędnego mapowania pozycji.

**Przykłady:**
- to samo SKU z różnymi stawkami VAT,
- to samo SKU z różnymi jednostkami,
- zmienna klasyfikacja tej samej pozycji.

**Severity:** `WARN`

---

### P2.13. Cena jednostkowa nietypowa
**Cel:** wykrycie błędów lub anomalii cenowych.

**Porównania:**
- do mediany historycznej,
- do zakresu akceptowalnego dla SKU / kategorii.

**Severity:** `WARN`

---

### P2.14. Ilości nietypowe
**Cel:** wykrycie pomyłek ilościowych.

**Przykłady:**
- ilość ułamkowa przy towarze sprzedawanym zwykle w sztukach,
- bardzo wysoka ilość,
- ilość ujemna bez korekty.

**Severity:** `WARN`

---

# P3 — warstwa anomalii i ryzyk audytowych

## 11. Walidacje anomalii biznesowych

### P3.1. Dokument nietypowy względem historii
**Cel:** wykrycie dokumentów odbiegających od wzorca.

**Przykłady:**
- nowy kontrahent z wysoką kwotą,
- nietypowa stawka VAT,
- wyjątkowo duża korekta,
- niestandardowy pattern dokumentu.

**Severity:** `WARN`
**Dodatkowo:** scoring ryzyka

---

### P3.2. Zmiany historyczne o podwyższonym ryzyku
**Cel:** wzmocnienie audytu zmian.

**Analizować:**
- liczbę ręcznych edycji,
- częstotliwość zmian,
- zmianę kluczowych pól po czasie,
- wpływ zmian na raportowany okres.

**Severity:** `WARN`

---

### P3.3. Heurystyki audytowe
**Przykłady:**
- dokument z weekendową datą od dostawcy, który zwykle tak nie wystawia,
- dwa bardzo podobne dokumenty o podobnej kwocie,
- wiele dokumentów ręcznie poprawianych w krótkim czasie.

**Severity:** `WARN`

---

## 12. Risk scoring dokumentu
**Cel:** priorytetyzacja ręcznego przeglądu.

**Model punktowy przykładowy:**
- `FAIL` krytyczny: +50
- `WARN` wysoki: +20
- `WARN` niski: +5

**Przedziały:**
- `0–19` → niski risk
- `20–49` → średni risk
- `50+` → wysoki risk

**Zastosowanie:**
- ranking dokumentów do ręcznego sprawdzenia,
- dashboard operacyjny,
- filtr do review przed zamknięciem miesiąca.

---

# Proponowana kolejność wdrożenia

## Faza 1 — fundament
Wdrożyć:
- kompletność pól,
- poprawność formatów,
- duplikaty,
- netto/VAT/brutto,
- suma pozycji vs nagłówek,
- poprawność VAT,
- daty i okresy,
- korekty vs dokument źródłowy,
- gotowość i zgodność JPK.

**Zakres:** P1

---

## Faza 2 — logika operacyjna
Wdrożyć:
- ciągłość numeracji,
- powiązania dokumentów,
- koszt ↔ płatność,
- kontrahenci,
- B2B/B2C,
- kraj vs VAT,
- koszty cykliczne,
- podstawowe walidacje SKU/cen/ilości.

**Zakres:** P2

---

## Faza 3 — warstwa ryzyka i analityki
Wdrożyć:
- anomalie historyczne,
- heurystyki audytowe,
- scoring ryzyka,
- dashboard dokumentów do review.

**Zakres:** P3

---

# Proponowany model reguły

## Przykład definicji reguły

```yaml
rule_id: VAT_001
name: Netto plus VAT równa się brutto
priority: P1
scope: invoice
severity: FAIL
condition: abs((net + vat) - gross) > tolerance
message: Niezgodność sum netto, VAT i brutto
suggested_action: Sprawdź pozycje dokumentu oraz zaokrąglenia
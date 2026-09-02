# Kontrola stanu okresu — „jak dziś stoję?"

**Data:** 2026-09-02
**Status:** Zatwierdzony projekt, implementacja w toku
**Zgłosił:** właściciel — „zamykanie miesiąca jest zbyt sztywne i przysparza więcej problemów"
**Zakres:** wystawić inspekcję okresu jako czysty odczyt, niezależny od przebiegu zamknięcia
**Rola w większej całości:** **pierwsza cegła przebudowanego zamykania miesiąca**, nie doklejka

---

## Problem

Żeby dowiedzieć się, jak stoi okres, trzeba dziś **wykonać akcję** `check`. Ta akcja:

- zajmuje okres (`try_claim`) — nikt inny nic w nim nie zrobi,
- wymaga roli księgowego lub admina; `viewer` nie sprawdzi nic,
- **trwale zmienia przebieg** — ustawia statusy kroków i status całości,
- jest krokiem, od którego bramkowane są wszystkie pozostałe (`workflow.py:908`).

Nie da się po prostu zapytać „jak dziś stoję z sierpniem?".

To jest **główne źródło odczuwanej sztywności**. Miesiąc zamyka się iteracyjnie — faktury spływają
tygodniami — więc naturalną czynnością jest zaglądanie, czy już komplet. Dziś każde takie
zajrzenie jest ruchem w maszynie stanów, a nie pytaniem.

### Co już istnieje i działa

`MonthCloseInspector.inspect()` (dziś w `workflow.py:173`) jest **już czystą funkcją**: listuje
pliki w blobie, pobiera faktury z Fakturowni i zwraca `{documents, issues, metrics}`. Nie zapisuje
plików, nie wysyła maili, nie dotyka przebiegu.

Jest wołana **wyłącznie ze środka `_perform`** — przy akcji `check` oraz przy odświeżeniu wejść
przed `package`/`send`. Zdolność istnieje; brakuje tylko drzwi.

Zawodzi też już bezpiecznie: gdy Fakturownia nie odpowiada, zgłasza `blocker`
`fakturownia-unavailable` i `ready: False`, zamiast zwrócić pustą listę udającą porządek
(`workflow.py:204-229`).

### Znalezisko poboczne — nie naprawiane tutaj

`GET /api/close/workflow` **nie jest odczytem**. `get_or_create` (`run_store.py:243`) zakłada
i zapisuje przebieg, gdy go nie ma, a przy etapie zawieszonym ponad 30 minut przestawia go na
`failed` i również zapisuje. Samo otwarcie zakładki zmienia stan.

Odnotowane świadomie. Nowy endpoint tego nie powtarza; naprawa istniejącego to osobna decyzja,
bo zmiana zachowania może dotknąć UI.

---

## Projekt

### 1. Wydzielenie inspekcji do własnego modułu

`MonthCloseInspector` przenosi się z `workflow.py` do **`zdrovena/month_closing/inspection.py`**.

Powód nie jest kosmetyczny. Skoro inspekcja ma być fundamentem przebudowanego zamykania, musi być
jednostką, którą da się zrozumieć i przetestować bez wciągania maszyny stanów. Dziś siedzi w pliku
na 1120 linii, razem z bramkami, waiverami i wysyłką maila — czyli dokładnie z tym, od czego ma
być niezależna. Po wydzieleniu `workflow.py` schodzi do ~720 linii, a `inspection.py` ma jedną
odpowiedzialność: **powiedzieć, jak wygląda okres**.

Przeniesienie jest **zachowujące zachowanie**. `workflow.py` importuje `MonthCloseInspector`
z nowego miejsca; istniejące testy muszą przejść bez zmian.

### 2. Endpoint

```
GET /api/close/inspection?year=YYYY&month=M
```

Rola: **`viewer` wzwyż** — sprawdzenie stanu to nie jest czynność księgowa.

Odpowiedź:

```jsonc
{
  "year": 2026,
  "month": 8,
  "computed_at": "2026-09-02T10:15:00+00:00",  // liczone teraz, nie zapamiętane
  "documents": [...],
  "issues": [...],
  "metrics": {"ready": false, ...},
  "run": null    // albo {status, steps, waivers}, gdy przebieg istnieje
}
```

**Twarde reguły:**

- Endpoint **nigdy nie zapisuje**. Żadnego `get_or_create`, żadnego `try_claim`, żadnego `save`.
- Stan przebiegu dokładany jest zwykłym `store.get()`, który zwraca `None`, gdy przebiegu nie ma.
- Działa dla okresu, w którym **nic jeszcze nie zaczęto** — to jest główny przypadek użycia.
- `computed_at` jest w odpowiedzi po to, żeby było widać, że wynik jest świeży, a nie odgrzany.

### 3. Bez cache'u

Każde wywołanie kosztuje dwa strzały w Fakturownię i dwa listowania blobów. Cache kusi, ale:
API chodzi na wielu replikach, więc cache w pamięci byłby niespójny, a cache w tabeli to kolejny
stan do unieważniania — czyli dokładnie ta złożoność, którą ten projekt ma zmniejszać.

Decyzja: **bez cache'u, dopóki pomiar nie pokaże, że boli.** Gdyby bolało, właściwą odpowiedzią
jest najpierw zmierzyć, co konkretnie jest wolne.

### 4. Frontend

Panel **„Stan okresu"** w `CloseView`, widoczny bez uprawnień do uruchamiania kroków. Pokazuje:
czego brakuje, co się nie zgadza, ile dokumentów jest w komplecie, oraz `computed_at`.

Kluczowe: panel **nie ma przycisku, który cokolwiek uruchamia**. To jest miejsce, gdzie się
patrzy, nie gdzie się działa.

### 5. Czego świadomie tu nie ma

- Bramki (`_validate_action`) — nietknięte.
- Waivery — nietknięte.
- Efekt uboczny w `GET /workflow` — odnotowany, nienaprawiony.
- Kontrola poprawności liczb, ślad audytowy, webhooki zamiast pollerów — osobne wątki.

Powód jest jeden: bramki są **jedynym zabezpieczeniem przed wysłaniem księgowej złej paczki**.
Wchodzi się tam z konkretnym przypadkiem w ręku, nie przy okazji.

---

## Scenariusze testowe

| # | Scenariusz | Oczekiwanie |
|---|---|---|
| 1 | Okres bez żadnego przebiegu | 200, `run: null`, dokumenty i problemy policzone |
| 2 | Po wywołaniu — czy coś powstało? | **Żadnego zapisu**: przebiegu nie ma przed i nie ma po |
| 3 | Okres z istniejącym przebiegiem | Stan przebiegu dołączony, **bez modyfikacji** (`rev` i `updated_at` bez zmian) |
| 4 | Przebieg z zawieszonym etapem >30 min | Endpoint **nie** przestawia go na `failed` — inaczej niż `GET /workflow` |
| 5 | Rola `viewer` | 200 — sprawdzenie stanu nie wymaga uprawnień księgowego |
| 6 | Brak uwierzytelnienia | 401/403 jak reszta API |
| 7 | Fakturownia niedostępna | `blocker` `fakturownia-unavailable`, `ready: false` — nie pusta lista |
| 8 | `computed_at` obecne i świeże | Dwa wywołania → dwa różne znaczniki |
| 9 | Miesiąc poza zakresem (0, 13, rok < 2020) | 422 z walidacji |
| 10 | Wydzielenie modułu | Wszystkie istniejące testy zamykania przechodzą bez zmian |

Scenariusze 2, 3 i 4 są najważniejsze — to one pilnują, że „odczyt" naprawdę jest odczytem.

---

## Co dalej

Ten endpoint jest fundamentem. Jeśli sprawdzi się w użyciu, przebudowane zamykanie miesiąca
wyrasta z niego: inspekcja staje się **modelem odczytu**, a kroki i bramki — cienką warstwą decyzji
nad nim, zamiast być miejscem, w którym inspekcja jest uwięziona.

Osobno, poza tym dokumentem, czeka zgłoszony defekt: **do paczki trafia wygenerowany PDF mimo
dostępnego oryginału**. To diagnoza, nie projekt.

# Kolejka issue — od najmniejszej roboty

Kolejność, w jakiej rozwiązujemy otwarte issue. Ustalona 2026-08-28 po przeczytaniu
każdego z nich, nie po tytułach.

> **Ten plik posiada wyłącznie kolejność i oszacowanie.** Treść, status i kryteria
> akceptacji żyją w GitHubie i tam są prawdą. Nie kopiuj tu opisów — druga kopia
> rozjedzie się z oryginałem, a to dokładnie ta choroba, którą leczyły #279, #278
> i #310.
>
> Sprawdzenie, czy kolejka nie odstaje od rzeczywistości:
> ```
> gh issue list --state open --limit 100 --json number -q '.[].number' | sort -n
> ```

---

## Zrobione, czeka tylko na zamknięcie

Kod jest na produkcji. Otwarte, bo `Closes` nie działa przy merge'u do `develop` —
GitHub zamyka issue tylko przy gałęzi domyślnej.

- [x] [#335](https://github.com/PiotrGry/zdrovena-reconciliation/issues/335) — MSAL `interaction_in_progress` (PR #336, na produkcji)
- [x] [#310](https://github.com/PiotrGry/zdrovena-reconciliation/issues/310) — awaria storage ≠ brak danych (PR #333, na produkcji)
- [x] [#278](https://github.com/PiotrGry/zdrovena-reconciliation/issues/278) — lockfile w CI i Dockerze (PR #334, **czeka na scalenie**)

---

## 1. Godzina albo mniej

- [x] [#314](https://github.com/PiotrGry/zdrovena-reconciliation/issues/314) — **P2** ukryć moduł kosztów za flagą
  Mechanizm już istnieje. `features.js` ma `orders/products/users: false`, a `App.jsx`
  opakowuje je w `...(FEATURES.x && {...})`. Tylko `costs: CostView` (App.jsx:27) nie
  jest opakowane — dlatego placeholder widać w nawigacji. Trzy linie plus test.

- [x] [#216](https://github.com/PiotrGry/zdrovena-reconciliation/issues/216) — **P2** jedno źródło wersji
  Cztery rozjechane miejsca: `pyproject.toml`, `zdrovena/__init__.py:3`,
  `zdrovena/api/main.py:153`, `frontend/package.json`. Odczyt runtime przez
  `importlib.metadata`. Rozjazd potwierdzony w praktyce 2026-08-27.

- [x] [#315](https://github.com/PiotrGry/zdrovena-reconciliation/issues/315) — **P2** health check Fakturowni pod Key Vault
  Pięć wystąpień `FAKTUROWNIA_API_TOKEN`. Health check ma używać tego samego
  `get_secret()`, co runtime, zamiast czytać env.

## 2. Dzień pracy

- [x] [#213](https://github.com/PiotrGry/zdrovena-reconciliation/issues/213) — **P1** szum logów Azure SDK i OTel
  Ponad 99% `AppTraces` to szum. Filtry loggerów są proste; kryterium akceptacji
  wymaga porównania wolumenu w oknie czasowym, czyli czekania.

- [x] [#214](https://github.com/PiotrGry/zdrovena-reconciliation/issues/214) — **P1** alerty operacyjne
  Odblokowane przez #310, które dostarczyło zdarzenie `storage_unavailable`.
  Głównie Terraform i reguły alertów.

- [x] [#238](https://github.com/PiotrGry/zdrovena-reconciliation/issues/238) — **P1** obraz Docker i czas builda
  **Przejrzeć zakres przed startem.** PR #334 zmienił obraz na multi-stage, czyli
  połowę tego issue. Może zostać sam audyt cache.

- [x] [#217](https://github.com/PiotrGry/zdrovena-reconciliation/issues/217) — **P2** Activity Log do LAW
  Terraform plus KQL do runbooka. Wymaga dostępu do subskrypcji.

## 3. Kilka dni

- [x] [#316](https://github.com/PiotrGry/zdrovena-reconciliation/issues/316) — **P1** full-partition scan w ShippingStore
  Pięć miejsc robi dziś pełny skan partycji. Trzeba zaprojektować targeted lookup
  i klucze deduplikacji.

- [x] [#311](https://github.com/PiotrGry/zdrovena-reconciliation/issues/311) — **P1** integralność paczki month-close
  Wysokie stawki: ta paczka idzie do księgowej.

- [x] [#308](https://github.com/PiotrGry/zdrovena-reconciliation/issues/308) — **P2** audyt magazynowy przy zamknięciu miesiąca
  Nowa logika biznesowa, nie poprawka.

- [x] [#138](https://github.com/PiotrGry/zdrovena-reconciliation/issues/138) — **P1** zmienne Terraform i approval flow
- [x] [#215](https://github.com/PiotrGry/zdrovena-reconciliation/issues/215) — **P1** prywatna sieć w Terraformie
  Oba wymagają dostępu do Azure i ostrożności.

## 4. Największe

- [x] [#312](https://github.com/PiotrGry/zdrovena-reconciliation/issues/312) — **P1** duplikaty maili po crashu
  **Największa nieodwracalna szkoda z całej listy** — klient albo księgowa dostaje
  drugi mail. Trwały rekord próby ze stanami, fingerprint odbiorców, semantyka
  recovery, wspólny mechanizm dla Damage i Month Close. Nie „przy okazji".

- [x] [#317](https://github.com/PiotrGry/zdrovena-reconciliation/issues/317) — **P2** Damage do warstwy aplikacji (551 linii, najmniejszy z refaktorów)
- [ ] [#318](https://github.com/PiotrGry/zdrovena-reconciliation/issues/318) — **P2** rozbić ShippingView (2047 linii; ubyło — wyciągnięte 3 komponenty)
- [ ] [#313](https://github.com/PiotrGry/zdrovena-reconciliation/issues/313) — **P2** rozbić webhooks.py (2063 linie; **urosło** przy #294)

---

## Spoza listy issue

- [ ] **„Release validation" na `main` pada od dawna.** Celuje w staging, którego Static
  Web App (`zdrovena-frontend-staging`) nie istnieje, więc suita przerywa się na
  przygotowaniu środowiska i **nic nie testuje**. Produkuje czerwony krzyżyk przy każdym
  wydaniu, czyli uczy ignorowania czerwonego. Brak issue — założyć.

---

## Zasada kolejności

Sortowane po nakładzie pracy, bo o to prosił właściciel. Jeden wyjątek wart pamiętania:
**#312 jest ostatnie kosztem, a pierwsze ryzykiem.** Jeśli kiedykolwiek trzeba będzie
wybrać między „szybko" a „ważnie", to ono wygrywa.

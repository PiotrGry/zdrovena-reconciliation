# Do wykonania na żywej infrastrukturze

Trzy rzeczy, których nie da się domknąć z repozytorium. **Żadna nie wymaga
tworzenia nowych zasobów** — to weryfikacja i konfiguracja tego, co już jest.

Ten dokument istnieje, żeby nie zniknęły razem z zamkniętymi issue.

---

## 1. Severity kontroli magazynowych (#308)

Kontrole „faktura bez WZ" i „WZ bez faktury" trafiają dziś na listę problemów
operatora jako **`warning`**, nie `blocker`.

**Dlaczego tak:** issue sugerowało `blocker`, ale warunkowało to policzeniem, ile
ostatnich miesięcy już nie przechodzi. Bez danych z produkcji `blocker` w ciemno
mógłby zablokować każde zamknięcie od pierwszego dnia.

**Co zrobić:**

```
zdrovena audit -y 2026 -m 06
zdrovena audit -y 2026 -m 05
zdrovena audit -y 2026 -m 04
```

Policz `Invoices without WZ` i `Orphan WZ` w każdym miesiącu.

| Wynik | Decyzja |
| --- | --- |
| Pojedyncze sztuki albo zero | przestaw na `blocker` |
| Kilkanaście i więcej | zostaw `warning`, najpierw uporządkuj zaległości |

Przestawienie: zmienna środowiskowa Container App

```
MONTH_CLOSE_WAREHOUSE_SEVERITY=blocker
```

**Koszt: zero.** To zmiana wartości zmiennej środowiskowej.

---

## 2. Plan dla sieci prywatnej (#215)

ADR 0003 wybrał Service Endpoints i usunął nieużywane zasoby Private Endpoint.
Plan dla `enable_private_network=false` przeszedł w CI (`0 to destroy`, czyli
usunięte zasoby faktycznie nigdy nie były wdrożone). Plan dla `true` wymaga
dostępu do subskrypcji i backendu stanu.

```
cd infra/terraform
terraform plan -var enable_private_network=true -out=tfplan-private
```

**Przeczytaj przedtem sekcję „Ograniczenia i zagrożenia" w
`docs/ADR/0003-private-network-model.md`.** Dwie rzeczy są tam istotne:

- włączenie flagi **odtwarza** Container Apps Environment — migracja do VNetu nie
  jest zmianą w miejscu, planuj jak przerwę w działaniu,
- `default_action=Deny` odcina lokalne `az storage` i ręczne wgrywanie plików
  z laptopa. To jest cel, ale trzeba o tym wiedzieć przed włączeniem.

**Koszt: sam plan jest darmowy.** Zastosowanie go to ~€3/mies. (ruch VNet);
Service Endpoints są bezpłatne. Flaga zostaje `false`, dopóki tego świadomie nie
zmienisz — plan jest po to, żeby wiedzieć, co by się stało, a nie żeby włączać.

---

## 3. Potwierdzenie dostarczenia alertu (#214, #217)

Reguły alertów i eksport Activity Logu są w Terraformie i przeszły `plan`.
Nie potwierdzono jeszcze, że powiadomienie faktycznie dociera na skrzynkę.

**Najtańszy scenariusz** — istniejąca sonda DLQ, bez psucia niczego:

1. Wywołaj `POST /api/__test__/shipping/dlq` na **staging** (endpoint jest
   fail-closed w produkcji).
2. Poczekaj na e-mail z action group `zdrovena-ag-ops`.
3. Odczekaj **jeszcze do 15 minut** — Activity Log trafia do LAW z opóźnieniem
   i rekord `Activated` nie pojawia się od razu po powiadomieniu.
4. Uruchom zapytanie „Historia aktywacji i rozwiązań" z
   `docs/devops/alerty-operacyjne.md` — musi zwrócić rekord `Activated`.
5. Poczekaj na samoczynne rozwiązanie (`auto_mitigation_enabled = true`)
   i potwierdź rekord `Resolved`.

**Koszt: zero.** Sonda korzysta z istniejących zasobów; ingestia kilku rekordów
Activity Logu to ułamek grosza.

---

## Czego świadomie NIE robimy

Nie dokładamy zasobów płatnych, żeby domknąć te punkty. W szczególności:

- **bez** Private Endpointów (~€29/mies.) — decyzja w ADR 0003,
- **bez** Premium ACR (~€159/mies.) wymaganego przez PE dla rejestru,
- **bez** dodatkowych workspace'ów, środowisk i action groupów,
- retencja LAW zostaje na 30 dniach, ustawiona w jednym miejscu (`main.tf`).

Wolumen Activity Logu dla subskrypcji tej wielkości to rząd pojedynczych MB
miesięcznie. Sprawdzenie po pierwszym pełnym miesiącu — zapytanie o wolumen jest
w `docs/devops/alerty-operacyjne.md`.

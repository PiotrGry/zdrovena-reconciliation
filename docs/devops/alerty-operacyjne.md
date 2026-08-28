# Alerty operacyjne — parametry i triage

Wszystkie reguły są w `infra/terraform/monitoring.tf` i podpięte pod jeden
action group `${prefix}-ag-ops` (e-mail właściciela). Pilnuje tego test
`tests/test_alert_event_names.py`.

## Tabela alertów

| Alert | Sygnał | Okno | Częstotliwość | Próg | Sev |
| --- | --- | --- | --- | --- | --- |
| `alert-error-rate` | metryka `requests/failed` | PT5M | PT5M | > 5 | 1 |
| `alert-latency` | metryka `requests/duration` (średnia) | PT5M | PT5M | > 3000 ms | 2 |
| `alert-dlq-backlog` | `dlq.enqueued` | PT15M | PT5M | > 0 | 1 |
| `alert-no-tracking` | `shipping.orders_without_tracking_snapshot` | PT1H | PT30M | > 0 | 2 |
| `alert-poller-failing` | `exceptions` roli pollera | PT1H | PT15M | > 2 | 2 |
| `alert-storage-unavailable` | `storage_unavailable` | PT15M | PT5M | > 0 | 1 |
| `alert-dependency-failures` | `dependencies`, `success == false` | PT15M | PT5M | > 5 | 2 |
| `alert-sync-failed` | `sync.completed` z błędem | PT1H | PT15M | > 0 | 2 |
| `alert-stuck-execution` | `shipping.stuck_execution_snapshot` | PT1H | PT30M | > 1 | 1 |
| `alert-kaucja-divergence` | `kaucja_source_divergence` | PT6H | PT30M | > 0 | 3 |

Severity: 0 krytyczny, 1 błąd, 2 ostrzeżenie, 3 informacja, 4 szczegół.

## Dlaczego te progi, a nie zero

Próg zero jest uzasadniony **tylko** dla zdarzeń, które z konstrukcji nie
powstają przy poprawnej pracy. Tam, gdzie sygnał ma naturalny szum, próg zero
oznacza alert wyciszony po tygodniu, czyli gorzej niż brak alertu.

- **`storage_unavailable` — próg 0.** Zdarzenie powstaje wyłącznie w
  `storage_unavailable()` (`zdrovena/common/exceptions.py`). Brak encji to
  `ResourceNotFound` obsługiwany osobno i **nie** emitujący tego zdarzenia —
  rozróżnienie wprowadzone w #310.
- **`dependency-failures` — próg 5/15 min i wykluczone kody.** Wykluczamy
  `404`, `409`, `412`, bo to normalny przepływ sterowania tego kodu, nie awarie:
  404 to wzorzec „spróbuj odczytać, obsłuż brak" w `get_entity`, 409 to konflikt
  upsertu ponawiany automatycznie, a 412 to **przegrany wyścig o ETag** w
  `try_claim_execution`, czyli działający mechanizm zapobiegania podwójnej
  przesyłce. Bez wykluczenia reguła alarmowałaby non stop.
- **`stuck-execution` — próg > 1, czyli dwie kolejne migawki.** Jedyny znany
  fałszywy alarm: `execution_started_at` zapisuje moment PIERWSZEGO startu
  i jest celowo zachowywane przy ponowieniu (pinuje to
  `test_retry_preserves_original_execution_start`), więc draft ponawiany po
  starej porażce przez kilka sekund niesie stary znacznik. Kilka sekund nie
  przeżyje dwóch cykli pollera; realne zakleszczenie przeżyje każdy.
- **`sync-failed` — próg 0.** `sync.completed` powstaje ZAWSZE, także gdy jedno
  ze źródeł się wywaliło; wyjątek jest łapany i ląduje w payloadzie. Samo
  wystąpienie zdarzenia nic nie mówi — dlatego zapytanie zagląda do środka.
  `shopify.skipped` to nie błąd. `allegro.error == "credentials_not_configured"`
  alarmuje świadomie: synchronizacja, która po cichu nic nie robi, jest gorsza
  od takiej, która głośno pada.

## Triage

Każde zdarzenie z `zdrovena.events` niesie `correlation_id`. Od niego zaczyna
się każdy triage — spina request operatora, wywołania zależności i wyjątek
w jeden ciąg:

```kusto
union traces, exceptions, dependencies, requests
| where TimeGenerated between (datetime(<START>) .. datetime(<KONIEC>))
| extend cid = coalesce(
    tostring(parse_json(message).correlation_id),
    tostring(customDimensions["correlation_id"]),
    operation_Id
  )
| where cid == "<CORRELATION_ID>"
| project TimeGenerated, itemType, cloud_RoleName, message, resultCode, name
| order by TimeGenerated asc
```

| Alert | Pierwszy krok | Zamknięcie |
| --- | --- | --- |
| `storage-unavailable` | sprawdź stan konta Storage i uprawnienia tożsamości zarządzanej; `error_type` w payloadzie mówi, co poszło nie tak | ustaje samo po odzyskaniu dostępu (`auto_mitigation_enabled`) |
| `dependency-failures` | pogrupuj po `target` i `resultCode` — jeden dostawca czy wszystkie? | jeśli to jeden przewoźnik: sprawdź jego status; jeśli wszystkie: sieć/tożsamość |
| `sync-failed` | odczytaj `allegro_error` / `shopify_error` z alertu — treść wyjątku jest w payloadzie | powtórz synchronizację z portalu i potwierdź brak błędu |
| `stuck-execution` | `draft_ids` z payloadu → sprawdź każdy przez `GET /api/shipping/drafts/{id}`, **stan z API jest rozstrzygający** | jeśli przesyłka powstała u przewoźnika: dokończ ręcznie; jeśli nie: przestaw draft na `error` i wykonaj ponownie |
| `kaucja-divergence` | porównaj `native_kaucja` z `heuristic_kaucja` i sprawdź, czy katalog produktów się nie zmienił | popraw heurystykę albo katalog; faktury z rozjazdem wymagają korekty |

Draftu tkwiącego w `executing` **nie wolno** przestawiać na `pending` — to
przejście jest zabronione przez maszynę stanów (`ALLOWED_TRANSITIONS`).
Dozwolone wyjścia to `created` i `error`.

## Kontrolowane scenariusze testowe

Każdy scenariusz da się odpalić bez czekania na prawdziwą awarię. Wszystkie
poza pierwszym wymagają środowiska staging.

| Alert | Scenariusz |
| --- | --- |
| `dlq-backlog` | istniejąca sonda `test_probe=True` w `webhooks.py` |
| `storage-unavailable` | odbierz tożsamości Container App rolę Table Data Contributor na czas jednego cyklu; przywróć po alercie |
| `dependency-failures` | ustaw `INPOST_BASE_URL` na nieistniejący host i wywołaj utworzenie przesyłki 6 razy |
| `sync-failed` | ustaw `ALLEGRO_REFRESH_TOKEN` na wartość nieważną i kliknij „synchronizuj" |
| `stuck-execution` | wstaw draft z `status="executing"` i `execution_started_at` sprzed 5h; odczekaj **dwa** cykle pollera (40 min) |
| `kaucja-divergence` | faktura Allegro z kaucją natywną różną od liczby butelek PET |
| `no-tracking` | draft bez `tracking_number` z `created_at` sprzed 49h |

## Do potwierdzenia po wdrożeniu

Kryterium „potwierdzono dostarczenie co najmniej jednego testowego
powiadomienia" wymaga żywej infrastruktury i skrzynki odbiorczej. Najtańszy
scenariusz to `dlq-backlog` przez istniejącą sondę. **Nie jest to zamknięte
przez ten PR** — Terraform opisuje reguły, ale dostarczenia e-maila nie da się
sprawdzić z repozytorium.

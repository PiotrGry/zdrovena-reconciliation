# Szum logów w Log Analytics

Ponad 99% rekordów `AppTraces` pochodziło z bibliotek, nie z aplikacji (#213).
Ten dokument opisuje, skąd się to brało, co to ogranicza i jak zmierzyć efekt.

## Przyczyna

Azure Monitor distro podpina swój handler eksportujący pod **root loggera** —
`logger_name` w `configure_azure_monitor` domyślnie ma wartość `""`. Każdy rekord
INFO wyprodukowany przez dowolną bibliotekę w procesie leci więc do Log Analytics
i jest fakturowany.

Wcześniejsza obrona to była lista pięciu nazw loggerów przypiętych do `WARNING`,
w dodatku skopiowana do dwóch plików. Lista nazw obejmuje tylko te loggery,
o których ktoś pomyślał; kolejna gadatliwa zależność przychodzi spoza listy.

## Co to ogranicza

`zdrovena/common/logging_setup.py`, dwa niezależne pokrętła:

| Funkcja | Zakres | Zmienna środowiskowa | Domyślnie |
| --- | --- | --- | --- |
| `quiet_sdk_loggers()` | konsola i eksport, lista nazw SDK | `LOG_LEVEL_AZURE` | `WARNING` |
| `install_export_filter()` | **wyłącznie** handler eksportujący | `LOG_LEVEL_AZURE_EXPORT` | `WARNING` |

Filtr eksportu działa na wadze rekordu, nie na nazwie loggera:

- rekordy z `zdrovena.*` idą do LAW **zawsze**, na każdym poziomie — łącznie
  z `zdrovena.events` (`draft.created`, `shipment.created`, `sync.completed`),
- rekordy z pozostałych loggerów idą do LAW od `WARNING` w górę, więc awaria
  zależności zostaje widoczna, a jej sukcesy już nie.

Filtr nie dotyka handlerów konsolowych. Ograniczenie kosztu ingestii nie może
ograniczać tego, co widzi programista w `docker compose logs`.

## Konfiguracja per środowisko

Przez `extra_env` w module `container_app` (tak samo jak reszta ustawień
środowiskowych, patrz `infra/terraform/compute.tf`):

```hcl
extra_env = {
  LOG_LEVEL_AZURE_EXPORT = "INFO" # tylko na czas diagnozy incydentu
}
```

Wartość nierozpoznana jako poziom logowania cofa się do `WARNING` i zapisuje
o tym warning — literówka nie może po cichu wyłączyć całego potoku.

## Pomiar wolumenu przed i po

Okno 7-dniowe, rozbicie na aplikację i resztę. Uruchom **przed** wdrożeniem
i tydzień po, na tym samym zakresie dni tygodnia.

```kusto
AppTraces
| where TimeGenerated > ago(7d)
| extend Logger = tostring(Properties["logger_name"])
| summarize Records = count() by Source = iff(Logger startswith "zdrovena", "aplikacja", "biblioteki")
| extend Udzial = round(100.0 * Records / toscalar(
    AppTraces | where TimeGenerated > ago(7d) | count
  ), 2)
| order by Records desc
```

Najgłośniejsze loggery — do sprawdzenia, czy coś nowego nie wyrosło:

```kusto
AppTraces
| where TimeGenerated > ago(7d)
| extend Logger = tostring(Properties["logger_name"])
| summarize Records = count() by Logger, SeverityLevel
| order by Records desc
| take 25
```

Kontrola, że zdarzenia biznesowe **nie** zniknęły — ta kwerenda musi zwracać
niepuste wyniki po wdrożeniu:

```kusto
AppTraces
| where TimeGenerated > ago(7d)
| extend Logger = tostring(Properties["logger_name"])
| where Logger == "zdrovena.events"
| extend Event = tostring(parse_json(Message)["event"])
| summarize Records = count() by Event
| order by Records desc
```

Kontrola, że błędy zależności nadal docierają:

```kusto
AppTraces
| where TimeGenerated > ago(7d)
| extend Logger = tostring(Properties["logger_name"])
| where Logger !startswith "zdrovena" and SeverityLevel >= 2
| summarize Records = count() by Logger
| order by Records desc
```

## Kryterium akceptacji, którego nie da się zamknąć w teście

Spadek wolumenu o ≥90% mierzy się na żywym Log Analytics, w porównywalnym oknie
czasowym. Testy jednostkowe pinują zachowanie filtra i to, że lista loggerów nie
rozjedzie się znowu na dwie kopie — samego wolumenu nie zmierzą.

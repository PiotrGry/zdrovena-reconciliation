# ADR 0001: Granice modułów

## Date
2026-01-23 

## Authors
Piotr Gryzło

## Decyzja
Utrzymujemy wyraźne granice modułów: każdy moduł ma jeden publiczny entrypoint (np. orchestrator/service),
a wspólne elementy trafiają do wspólnego pakietu (np. common).

## Konsekwencje
- łatwiejsze testowanie
- mniej zależności krzyżowych
- łatwiejsze skalowanie repo


## Dodatkowe zasady

- Moduły nie mogą mieć zależności cyklicznych.
- CLI zna tylko warstwę orchestrator.
- Moduły biznesowe nie znają CLI.
- common nie może zależeć od modułów wyższego poziomu.
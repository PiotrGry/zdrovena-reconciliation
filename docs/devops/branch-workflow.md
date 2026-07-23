# Branch and release workflow

Standardowa ścieżka każdej zmiany:

```text
issue → branch od develop → PR do develop → PR develop do main
```

## Standardowa zmiana

1. Utwórz issue z zakresem i kryteriami akceptacji.
2. Utwórz branch z aktualnego `develop`, np. `feature/123-opis`,
   `fix/123-opis` lub `docs/123-opis`.
3. Otwórz PR do `develop` i dodaj `Closes #123`.
4. Poczekaj na wymagany check `Fast gate / Quality Gate`.
5. Po merge do `develop` otwórz release PR `develop → main`.
6. Merge do `main` jest dozwolony dopiero po przejściu `CI Gate`, pełnego
   staging smoke/E2E i ewentualnego Terraform plan.

Direct push, force-push i usuwanie `main` są zabronione. Nie używaj
`--force-with-lease` jako skrótu procesu wydaniowego.

## Awaryjny hotfix

Preferowana ścieżka nadal prowadzi przez `develop`. Jeżeli `develop` zawiera
inne, jeszcze niewydawane zmiany i produkcja wymaga natychmiastowej poprawki:

1. Utwórz issue incydentowe z wpływem i planem rollbacku.
2. Utwórz `hotfix/<issue>-opis` z aktualnego `main`.
3. Otwórz PR do `main`.
4. Właściciel repozytorium po weryfikacji zakresu dodaje etykietę
   `hotfix-approved`. Bez niej `Release source` blokuje PR.
5. PR nadal musi przejść pełny `CI Gate`; hotfix nie pomija testów.
6. Po merge niezwłocznie przenieś ten sam commit do `develop` przez osobny PR,
   aby gałęzie nie rozjechały się.

Awaryjny hotfix nie zezwala na force-push ani obniżenie progów jakości.

# Obraz produkcyjny — rozmiar, cache i decyzje

Pomiary z 2026-08-29, Docker 29.1.3, `linux/amd64`, ten sam host.

## Przed i po

| Miara | Przed | Po | Zmiana |
| --- | --- | --- | --- |
| Rozmiar obrazu | 317,9 MB | 212,7 MB | **−105,2 MB (−33%)** |
| Build zimny (`--no-cache`) | 17 s | 12 s | −5 s |
| Build ciepły, zmiana tylko w kodzie | 12 s | 4 s | −8 s |

Etap multi-stage istniał już wcześniej (#278) — ten dokument opisuje, co zostało
po nim do zrobienia.

## Skąd wzięło się 105 MB

**107 MB kosztował jeden `RUN useradd … && chown -R app:app /app`.** `chown -R`
przepisuje każdy plik, którego dotknie, do nowej warstwy — a dotykał całego
`/app/.venv` (104 MB). Obraz niósł więc drugą, pełną kopię virtualenva, różniącą
się od pierwszej wyłącznie właścicielem.

Naprawa: użytkownik powstaje **przed** kopiowaniem, właściciel ustawiany jest
przez `COPY --chown` w tej samej warstwie. `/app` dostaje osobnego, płytkiego
`chown` (jeden katalog, zero bajtów w warstwie), żeby proces mógł tam pisać tak
jak wcześniej.

**3,2 MB kosztowała druga kopia pakietu.** `uv sync --no-editable` instaluje
`zdrovena` do virtualenva, a `COPY zdrovena/ zdrovena/` w etapie finalnym
dokładał ją drugi raz do `/app`. Gorzej: `WORKDIR /app` jest na `sys.path`, więc
kopia z `/app` **przesłaniała** zainstalowaną — kod, który się uruchamiał, nie
był tym artefaktem, który zbudowano, przeskanowano i zweryfikowano.

Sprawdzenie po zmianie:

```
$ docker run --rm --entrypoint python3 <obraz> -c "import zdrovena; print(zdrovena.__file__)"
/app/.venv/lib/python3.12/site-packages/zdrovena/__init__.py
```

Do `.dockerignore` dopisany został `zdrovena/.continue/` — katalog roboczy
narzędzia deweloperskiego, który leżał wewnątrz drzewa pakietu i jechał na
produkcję.

## Cache

Układ warstw jest bez zmian i nadal deterministyczny:

1. `pyproject.toml` + `uv.lock` → `uv sync --locked --no-install-project`
2. `zdrovena/` → `uv sync --locked --no-editable`

Zmiana wyłącznie w kodzie aplikacji unieważnia tylko warstwę drugą. Ciepły build
po zmianie w kodzie skrócił się z 12 s do 4 s, bo nie ma już rekursywnego
`chown` przeliczanego za każdym razem.

GitHub Actions cache (`type=gha,mode=max`) zostaje bez zmian.

## Decyzja: zostajemy przy ręcznym `docker buildx build`

Punkt 4 zakresu #238 kazał sprawdzić, czy warto przejść na
`docker/build-push-action` + `docker/metadata-action`. **Nie warto**, i to jest
decyzja, nie odkładanie:

- akcje są już przypięte po SHA (`setup-buildx-action`), więc migracja nie
  poprawia bezpieczeństwa łańcucha dostaw,
- cache GHA `mode=max` jest już włączony, więc nie ma zysku na czasie,
- tagowanie jest proste (dwa tagi na staging), a `metadata-action` wnosi tu
  głównie konfigurację.

Jedyną realną korzyścią były **standardowe etykiety OCI**, i te zostały dodane
wprost do istniejącego wywołania, bez brania nowej zależności:
`org.opencontainers.image.source`, `.revision`, `.created`. Dodane też
`--platform linux/amd64`, bo Container Apps i tak działają na tej architekturze
i lepiej mieć to zapisane niż odziedziczone po runnerze.

## Promocja artefaktu

Bez zmian i już poprawna: `scripts/ci/promote-image.sh` robi `docker pull` obrazu
staging i przetagowuje go (`docker tag`), nie przebudowuje. Produkcja dostaje
dokładnie ten sam artefakt, który przeszedł staging, razem z etykietami.

## Co pilnuje, żeby to zostało prawdą

`tests/test_docker_image_policy.py`. Wszystkie te własności psują się po cichu:
obraz cięższy o 100 MB nadal startuje, a pakiet skopiowany na samego siebie nadal
się importuje. Test sprawdza multi-stage, brak narzędzi budowania w etapie
finalnym, brak przesłaniającego `COPY`, brak `chown -R`, użytkownika non-root,
`HEALTHCHECK` i ścieżkę do entrypointu `zdrovena`.

# Sandbox setup — instrukcja refaktoru kodu

Status: draft, faza 3 (higiena) z `AUDIT_REPORT.pdf`.
Cel: umożliwić uruchamianie integracji InPost / Apaczka / Allegro na środowiskach testowych
bez modyfikacji kodu — przez zmienne środowiskowe i Key Vault.

Konfigurację po stronie paneli InPost/Apaczka/Allegro znajdziesz w osobnym dokumencie
(nie w repo — nie commituj go).

---

## 1. Stan obecny (problem)

URL-e produkcyjne są **hardcoded** w klientach:

| Plik | Linia | Wartość |
|---|---|---|
| `zdrovena/common/inpost.py` | 16 | `_BASE = "https://api-shipx-pl.easypack24.net"` |
| `zdrovena/common/apaczka.py` | 22 | `_BASE = "https://www.apaczka.pl/api/v2"` |
| `zdrovena/common/allegro.py` | — | brak modułu, brak integracji |

Sekrety pobierane przez `get_secret(...)` z Key Vault w `webhooks.py:139-140, 222-224, 538-539, 634-635, 641-642`.

Cel po refaktorze:
- jeden kod, dwa środowiska (prod / sandbox) sterowane env
- testy lokalne bez ryzyka wywołania prod API
- przygotowanie pod Allegro (klient zostanie dodany analogicznie)

---

## 2. Refaktor InPost — `zdrovena/common/inpost.py`

### 2.1. Zmień stałą `_BASE` na funkcję czytającą env

**Linia 16** — zamień:

```python
_BASE = "https://api-shipx-pl.easypack24.net"
```

na:

```python
import os

_BASE_PROD = "https://api-shipx-pl.easypack24.net"
_BASE_SANDBOX = "https://sandbox-api-shipx-pl.easypack24.net"


def _resolve_base_url() -> str:
    """Zwraca URL InPost ShipX API.

    Priorytet:
      1. INPOST_BASE_URL — pełny URL (najwyższy priorytet, do override w testach)
      2. INPOST_ENV=sandbox → URL sandbox
      3. domyślnie produkcja
    """
    override = os.getenv("INPOST_BASE_URL")
    if override:
        return override.rstrip("/")
    if os.getenv("INPOST_ENV", "").lower() == "sandbox":
        return _BASE_SANDBOX
    return _BASE_PROD
```

### 2.2. Użycie w klasie `InPostClient`

W `__init__` (linia 25) dodaj wybór URL:

```python
def __init__(self, api_token: str, organization_id: str, *, base_url: str | None = None) -> None:
    self._base = (base_url or _resolve_base_url()).rstrip("/")
    self._org_id = organization_id
    self._session = requests.Session()
    self._session.headers.update(
        {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    )
    logger.info("InPost client initialized: base_url=%s org=%s", self._base, organization_id)
```

### 2.3. Wszystkie wystąpienia `_BASE` zamień na `self._base`

Konkretnie linie **112, 143, 173** w `inpost.py`:

```python
# było:
url = f"{_BASE}/v1/organizations/{self._org_id}/shipments"
# ma być:
url = f"{self._base}/v1/organizations/{self._org_id}/shipments"
```

Powtórz dla dispatch_orders (143) i label (173).

### 2.4. Logowanie środowiska

Dodaj w log line w `__init__` (już jest w 2.2) — to ważne dla debugowania,
żeby w produkcji od razu widzieć w logach, że odwołujemy się do sandbox.

---

## 3. Refaktor Apaczka — `zdrovena/common/apaczka.py`

**Uwaga:** Apaczka **nie ma osobnego URL-a sandbox** (potwierdzone w dokumentacji v2).
Tryb testowy realizuje się przez drugie konto w Apaczce lub mocki w testach.
Mimo to dodajemy konfigurowalny `_BASE` na wypadek przyszłej zmiany i dla spójności architektury.

### 3.1. Linia 22 — zamień:

```python
_BASE = "https://www.apaczka.pl/api/v2"
```

na:

```python
import os

_BASE_PROD = "https://www.apaczka.pl/api/v2"


def _resolve_base_url() -> str:
    """Zwraca URL Apaczka API v2.

    Apaczka nie ma osobnego sandbox URL — wartość domyślna pasuje
    do produkcji. Override przez APACZKA_BASE_URL umożliwia testy z mock serverem.
    """
    return (os.getenv("APACZKA_BASE_URL") or _BASE_PROD).rstrip("/")
```

### 3.2. W `__init__` klasy `ApaczkaClient` (linia 46):

```python
def __init__(
    self,
    app_id: str,
    app_secret: str,
    service_id: str,
    storage: Any,
    *,
    base_url: str | None = None,
) -> None:
    self._base = (base_url or _resolve_base_url()).rstrip("/")
    self._app_id = app_id
    self._secret = app_secret
    self._service_id = service_id
    self._storage = storage
    self._session = requests.Session()
    logger.info("Apaczka client initialized: base_url=%s app_id=%s", self._base, app_id)
```

### 3.3. Wystąpienia `_BASE` → `self._base`

Linia **55** i wszystkie inne w pliku — wyszukaj `_BASE` w `apaczka.py` i zamień na `self._base`.

---

## 4. Nowy moduł Allegro — `zdrovena/common/allegro.py`

Jeśli integracja Allegro ma się pojawić, dodaj nowy plik zgodnie z konwencją InPost/Apaczka.
**Nie commituj kluczy** — używaj `get_secret(...)`.

### 4.1. Szkielet pliku

```python
"""zdrovena.common.allegro — Allegro REST API client.

OAuth 2.0 Client Credentials flow dla operacji aplikacji,
Authorization Code dla operacji sprzedawcy.

Secrets (Key Vault):
  - allegro-client-id
  - allegro-client-secret
  - allegro-refresh-token (po pierwszej autoryzacji sellera)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger("zdrovena.common.allegro")

_TIMEOUT = 15

_BASE_PROD = "https://api.allegro.pl"
_BASE_SANDBOX = "https://api.allegro.pl.allegrosandbox.pl"
_OAUTH_PROD = "https://allegro.pl/auth/oauth"
_OAUTH_SANDBOX = "https://allegro.pl.allegrosandbox.pl/auth/oauth"
_UPLOAD_PROD = "https://upload.allegro.pl"
_UPLOAD_SANDBOX = "https://upload.allegro.pl.allegrosandbox.pl"


def _resolve_urls() -> tuple[str, str, str]:
    """Zwraca (base_url, oauth_url, upload_url) wg ALLEGRO_ENV.

    ALLEGRO_ENV=sandbox → wszystkie URL-e sandbox.
    Domyślnie produkcja. Można nadpisać pojedyncze przez
    ALLEGRO_BASE_URL / ALLEGRO_OAUTH_URL / ALLEGRO_UPLOAD_URL.
    """
    env = os.getenv("ALLEGRO_ENV", "").lower()
    if env == "sandbox":
        base, oauth, upload = _BASE_SANDBOX, _OAUTH_SANDBOX, _UPLOAD_SANDBOX
    else:
        base, oauth, upload = _BASE_PROD, _OAUTH_PROD, _UPLOAD_PROD

    return (
        (os.getenv("ALLEGRO_BASE_URL") or base).rstrip("/"),
        (os.getenv("ALLEGRO_OAUTH_URL") or oauth).rstrip("/"),
        (os.getenv("ALLEGRO_UPLOAD_URL") or upload).rstrip("/"),
    )


class AllegroError(Exception):
    pass


class AllegroClient:
    """Allegro REST client z OAuth token cache."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        refresh_token: str | None = None,
        base_url: str | None = None,
        oauth_url: str | None = None,
    ) -> None:
        b, o, u = _resolve_urls()
        self._base = (base_url or b).rstrip("/")
        self._oauth = (oauth_url or o).rstrip("/")
        self._upload = u
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._session = requests.Session()
        logger.info("Allegro client initialized: base=%s oauth=%s", self._base, self._oauth)

    # ── OAuth ──────────────────────────────────────────────────────────────

    def _token(self) -> str:
        """Zwraca aktualny access token. Odświeża jeśli wygasł."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        if self._refresh_token:
            data = {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        else:
            data = {"grant_type": "client_credentials"}

        resp = requests.post(
            f"{self._oauth}/token",
            data=data,
            auth=(self._client_id, self._client_secret),
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            raise AllegroError(f"OAuth failed: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        self._access_token = body["access_token"]
        self._token_expires_at = time.time() + int(body.get("expires_in", 43200))
        if "refresh_token" in body:
            self._refresh_token = body["refresh_token"]
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.allegro.public.v1+json",
            "Content-Type": "application/vnd.allegro.public.v1+json",
        }

    # ── Examples (do uzupełnienia gdy ustalimy zakres integracji) ─────────

    def get_orders(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        url = f"{self._base}/order/checkout-forms"
        resp = self._session.get(
            url,
            headers=self._headers(),
            params={"limit": limit, "offset": offset},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            raise AllegroError(f"get_orders failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()
```

### 4.2. Integracja w `webhooks.py` / orchestrator

Wzorzec analogiczny do InPost:

```python
from zdrovena.common.allegro import AllegroClient

client_id = get_secret("allegro_client_id")
client_secret = get_secret("allegro_client_secret")
refresh_token = get_secret("allegro_refresh_token")  # tylko dla operacji sellera
client = AllegroClient(client_id, client_secret, refresh_token=refresh_token)
```

---

## 5. Zmienne środowiskowe — dokumentacja

### 5.1. Stwórz / zaktualizuj `.env.example`

W roocie repo (jeśli nie istnieje, utwórz):

```bash
# ── InPost ShipX ─────────────────────────────────────────────────────────
# Wybór środowiska. Wartości: sandbox | (puste = produkcja)
INPOST_ENV=sandbox
# Override pełnego URL-a (priorytet nad INPOST_ENV) — używaj w testach
# INPOST_BASE_URL=https://sandbox-api-shipx-pl.easypack24.net
# Sekrety (w produkcji idą przez Key Vault, lokalnie przez .env.local):
# INPOST_API_TOKEN=...
# INPOST_ORGANIZATION_ID=...

# ── Apaczka ──────────────────────────────────────────────────────────────
# Apaczka nie ma sandbox — używaj drugiego konta firmowego.
# APACZKA_BASE_URL=https://www.apaczka.pl/api/v2
# Sekrety:
# APACZKA_APP_ID=...
# APACZKA_APP_SECRET=...
# APACZKA_SERVICE_ID=...

# ── Allegro ──────────────────────────────────────────────────────────────
# Wybór środowiska. Wartości: sandbox | (puste = produkcja)
ALLEGRO_ENV=sandbox
# ALLEGRO_BASE_URL=https://api.allegro.pl.allegrosandbox.pl
# ALLEGRO_OAUTH_URL=https://allegro.pl.allegrosandbox.pl/auth/oauth
# ALLEGRO_UPLOAD_URL=https://upload.allegro.pl.allegrosandbox.pl
# Sekrety:
# ALLEGRO_CLIENT_ID=...
# ALLEGRO_CLIENT_SECRET=...
# ALLEGRO_REFRESH_TOKEN=...  # po pierwszej autoryzacji seller account
```

### 5.2. `.env.local` (NIE commituj — dodaj do `.gitignore`)

Plik **lokalny** z prawdziwymi sekretami z paneli sandbox:

```bash
INPOST_ENV=sandbox
INPOST_API_TOKEN=<token z sandbox-manager.paczkomaty.pl>
INPOST_ORGANIZATION_ID=<org id z sandbox-manager>

APACZKA_APP_ID=<app id drugiego konta firmowego>
APACZKA_APP_SECRET=<app secret>
APACZKA_SERVICE_ID=<service id>

ALLEGRO_ENV=sandbox
ALLEGRO_CLIENT_ID=<z apps.developer.allegro.pl>
ALLEGRO_CLIENT_SECRET=<j.w.>
```

Sprawdź `.gitignore`:

```bash
grep -E "^\.env\.local|^\.env\.\*" .gitignore || echo ".env.local" >> .gitignore
```

---

## 6. Aktualizacja Key Vault (deployment)

W produkcji nie używamy `.env` — sekrety są w Azure Key Vault. Sandbox można podpiąć na dwa sposoby:

**Opcja A: drugi Key Vault `kv-zdrovena-sandbox`** (rekomendowane)
- Skopiuj strukturę KV produkcyjnego, wrzuć sekrety sandbox
- W Terraform dodaj zmienną `key_vault_name` i steruj nią per environment
- Container App ma zmienne `INPOST_ENV=sandbox`, `ALLEGRO_ENV=sandbox`

**Opcja B: tylko lokalne testy**
- Sekrety sandbox tylko w `.env.local`, nigdy w Azure
- Najprostsze, ale uniemożliwia testy z deployowanej aplikacji

Sugestia: zacznij od opcji B. Jeśli zechcesz testować end-to-end na ephemeral env (faza 3
z audytu), wtedy zrób opcję A.

---

## 7. Testy

### 7.1. Test jednostkowy — sprawdza wybór URL

Dodaj do `tests/test_inpost.py` (lub utwórz jeśli nie ma):

```python
import os
from unittest.mock import patch

import pytest

from zdrovena.common.inpost import InPostClient, _resolve_base_url


class TestUrlResolution:
    def test_default_is_production(self, monkeypatch):
        monkeypatch.delenv("INPOST_ENV", raising=False)
        monkeypatch.delenv("INPOST_BASE_URL", raising=False)
        assert _resolve_base_url() == "https://api-shipx-pl.easypack24.net"

    def test_sandbox_env_switches_url(self, monkeypatch):
        monkeypatch.delenv("INPOST_BASE_URL", raising=False)
        monkeypatch.setenv("INPOST_ENV", "sandbox")
        assert _resolve_base_url() == "https://sandbox-api-shipx-pl.easypack24.net"

    def test_explicit_url_overrides_env(self, monkeypatch):
        monkeypatch.setenv("INPOST_ENV", "sandbox")
        monkeypatch.setenv("INPOST_BASE_URL", "https://custom.example.com")
        assert _resolve_base_url() == "https://custom.example.com"

    def test_constructor_base_url_wins(self):
        client = InPostClient("token", "org", base_url="https://override.test")
        assert client._base == "https://override.test"
```

Analogicznie dla Apaczki i Allegro.

### 7.2. Smoke test lokalny (manualny)

```bash
# w terminalu z załadowanym .env.local
python -c "
from zdrovena.common.inpost import InPostClient
import os
c = InPostClient(os.environ['INPOST_API_TOKEN'], os.environ['INPOST_ORGANIZATION_ID'])
print('Base URL:', c._base)
# sprawdza czy odpowiada — bez tworzenia przesyłki
import requests
r = requests.get(f'{c._base}/v1/organizations/{c._org_id}', headers=c._session.headers, timeout=10)
print('Status:', r.status_code)
print('Body:', r.text[:200])
"
```

Oczekiwane: `200` lub `404` (nie `Connection refused`, nie `dns error`).

---

## 8. Checklista PR

Tytuł: `refactor(common): make API base URLs configurable for sandbox testing`

- [ ] `zdrovena/common/inpost.py` — `_resolve_base_url()`, `__init__` z `base_url`, wszystkie `_BASE` → `self._base`
- [ ] `zdrovena/common/apaczka.py` — analogicznie
- [ ] `zdrovena/common/allegro.py` — nowy plik (jeśli wprowadzamy integrację)
- [ ] `.env.example` — dodane sekcje InPost / Apaczka / Allegro
- [ ] `.gitignore` — `.env.local` ignorowany
- [ ] `tests/test_inpost.py`, `tests/test_apaczka.py` — testy resolution URL-a
- [ ] `tests/test_allegro.py` — testy OAuth + URL resolution (jeśli dodajemy klienta)
- [ ] `docs/working/sandbox-setup-code-refactor.md` — ten plik (już commitnięty)
- [ ] `CHANGELOG.md` — wpis pod `## [Unreleased]`:
  > - refactor: configurable base URLs for InPost / Apaczka / Allegro via env (`INPOST_ENV`, `ALLEGRO_ENV`)
- [ ] Manual smoke test lokalny — InPost sandbox odpowiada

---

## 9. Kolejność wdrożenia

1. **Refaktor InPost** (15 min) — najmniejsze ryzyko, jest realny sandbox
2. **Testy unit InPost** (15 min) — walidują że domyślnie nadal idzie prod
3. **Refaktor Apaczka** (10 min) — tylko `_BASE` na env, kod nie zmienia zachowania
4. **`.env.example` + `.gitignore`** (5 min)
5. **CHANGELOG + PR + review** (15 min)
6. **Allegro** — osobny PR, jeśli faktycznie wprowadzamy integrację (1-2h razem z OAuth flow)

Łącznie InPost + Apaczka: **~1h pracy**.

---

Po commicie tego pliku, faktyczny refaktor robisz osobnym commit/PR — ten dokument jest
specyfikacją, nie implementacją. Implementacja jako odrębny PR żeby było czyste code review.

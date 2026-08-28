# Contributing

## Local setup

### Pre-commit hooks (required for code quality)

Lint and formatting checks run **locally before you commit**, not in CI. This keeps your PR unblocked and catches issues early.

1. Install pre-commit:
   ```bash
   pip install pre-commit
   ```

2. Install the git hooks:
   ```bash
   pre-commit install
   ```

3. Run checks before committing (automatic after install):
   ```bash
   pre-commit run --all-files
   ```

Checks that run locally:
- **Python**: ruff (linting + formatting)
- **Python**: pyright (type checking)
- **Frontend**: eslint (linting) + prettier (formatting)

If pre-commit fails, it will show you the issues. Most are auto-fixable — run the commands again after fixes are made.

## Branching strategy

```
main ← develop ← feature/your-feature
         ↑
    (production)
```

- **`main`** — production. Never commit directly. CI deploys to production on merge.
- **`develop`** — integration branch. All feature branches merge here first.
- **`feature/*`** — short-lived feature branches off `develop`. Delete after merge.

### Workflow

1. Branch off `develop`:
   ```bash
   git checkout develop && git pull
   git checkout -b feature/your-feature
   ```
2. Open a PR into `develop`. CI runs quality gate (lint, typecheck, tests, security).
3. Merge to `develop` → staging deploy + smoke tests run automatically.
4. Open a PR from `develop` into `main` to ship to production.
5. Merging to `main` → production deploy + semantic version bump.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(api): add GET /dashboard endpoint
fix(close): handle missing state.json gracefully
chore(ci): replace ruff with eslint after TS migration
```

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `ci`

A `feat:` commit bumps minor version. A `fix:` bumps patch. `BREAKING CHANGE:` in the
footer bumps major. Semantic release runs automatically on merge to `main`.

## Quality gate

### Local (automatic on push)

`.git/hooks/pre-push` runs `scripts/check.sh` on every push. It is not optional — it is the
last gate before code leaves your machine, and it blocks the push when a step fails.

Install the hook (one-time per developer):
```bash
bash scripts/install-hooks.sh
```

A change touching only `.md` / `.pdf` files takes the fast path in `scripts/docs-only.sh` and
skips the gate. Force a full run with `CHECK_DOCS_FASTPATH=0 bash scripts/check.sh`.

**A step that cannot run fails the gate.** A missing tool used to print a skip and let the push
through, which meant a half-built `.venv` silently downgraded every guarantee below (#279). If you
genuinely cannot install something, skip it on purpose with the matching variable — that way the
skip is a decision you recorded, not something that happened to you.

| Step | Needs | Skip with |
| --- | --- | --- |
| ruff lint + format | `uv sync --extra dev` | `CHECK_RUFF=0` |
| pyright | `uv sync --extra dev` | `CHECK_TYPECHECK=0` |
| bandit | `uv sync --extra dev` | `CHECK_BANDIT=0` |
| pytest + coverage | `uv sync --extra dev` | `CHECK_TESTS=0` |
| uv lock consistency | [uv](https://docs.astral.sh/uv/) | `CHECK_UV_LOCK=0` |
| pip-audit | `uv sync --extra dev` | `CHECK_PIPAUDIT=0` |
| SOPS age guard | `chmod +x scripts/check-sops-secrets.sh` | `CHECK_SOPS=0` |
| gitleaks | [gitleaks](https://github.com/gitleaks/gitleaks) | `CHECK_GITLEAKS=0` |
| trivy | [trivy](https://aquasecurity.github.io/trivy) | `CHECK_TRIVY=0` |
| terraform fmt | [terraform](https://developer.hashicorp.com/terraform/downloads) | `CHECK_TERRAFORM=0` |
| checkov | `uv sync --extra iac` | `CHECK_CHECKOV=0` |
| frontend lint, tests, build | `npm --prefix frontend ci` | `CHECK_FRONTEND=0` |

Use `npm ci`, not `npm install`. `npm install` resolves fresh versions and lets `node_modules`
drift from `package-lock.json`, which makes the local lint weaker than CI's — that is how an
eslint rule violation reached CI on a branch whose local lint was green.

`git push --no-verify` bypasses the whole gate. Prefer one targeted variable above.

### CI (after push to develop / on PR to main)

The GitHub Actions quality gate is now faster (no unit tests):

```bash
# Lint + format
ruff check . && ruff format --check .

# Type check
pyright

# Security (SAST)
bandit -r zdrovena/ -ll -ii -q
pip-audit
gitleaks
trivy
```

**Staging gate (PR → main):** runs TypeScript smoke tests + Playwright E2E against a real staging deployment. This is the final quality gate before production.

The frontend has its own lint gate (`cd frontend && npm run lint`) run via pre-commit.

## Storage error handling

A storage failure must never be answered with the value that means *absence*. Returning `None` or
`[]` when Azure is unreachable makes an outage read as "the record does not exist", and a caller
acting on that picture can write a duplicate — that is what issue #310 was about.

The rule, in two shapes:

- **Single-entity reads** catch `ResourceNotFoundError` from `azure.core.exceptions` and return
  `None`. Every other exception raises `storage_unavailable(store, operation, exc)`.
- **List reads** have no not-found case — an empty partition returns an empty result without
  raising — so *every* exception is an outage and raises.

`StorageUnavailableError` maps to **HTTP 503** with a correlation id, and
`storage_unavailable()` emits a `storage_unavailable` event for alerting. Never catch bare
`Exception` on a read path and return an empty value.

`zdrovena/common/shopify_dedup_store.py` is the reference implementation.

The local JSON backend is deliberately different: a mangled file reads as empty and the next
write rewrites it, so a developer is never stuck with an unusable store. Azure has no equivalent
self-healing, which is why the table paths raise.

## Local dev

```bash
bash dev.sh
```

This starts the FastAPI backend (`AZURE_AUTH_DISABLED=true`, port 8000) and the Vite frontend (port 5173) together. API docs are at http://localhost:8000/docs.

Set `AZURE_AUTH_DISABLED=true` to skip JWT validation locally — all requests are treated as `zdrovena-admin`.

## Roles

The app has three roles: `zdrovena-viewer`, `zdrovena-accountant`, `zdrovena-admin`.

- **zdrovena-viewer** — read-only access (dashboard, invoices, files)
- **zdrovena-accountant** — can trigger month-close pipeline + download
- **zdrovena-admin** — full access including user management

When adding a new endpoint, decide the minimum required role and use the appropriate
dependency from `zdrovena/api/auth.py`:

- `require_viewer_or_above` — read-only endpoints
- `require_accountant_or_admin` — write/close operations
- `require_admin` — admin-only operations

Never lock a GET endpoint behind `require_accountant_or_admin` unless it triggers side effects.

## Secrets

Never commit secrets. All secrets live in Azure Key Vault (production) or `.env.local`
(local dev, gitignored). See `zdrovena setup` for the secrets wizard.

## KSeF (Polish e-invoicing)

KSeF signing uses XML-DSIG. Test any signing changes against the KSeF test environment
before touching production. A single misplaced byte in the signature invalidates the
invoice. The KSeF sandbox URL is in `.env.local` as `KSEF_TEST_URL`.

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

Every PR must pass. Run locally with:

```bash
bash scripts/check.sh
```

This runs:

```bash
# Lint
ruff check . && ruff format --check .

# Type check (optional locally — slow cold start ~30s)
# Enable with: CHECK_TYPECHECK=1 bash scripts/check.sh
pyright

# Tests (≥80% coverage)
pytest --cov=zdrovena --cov-fail-under=80

# Security (SAST)
bandit -r zdrovena/ -ll -ii -q
```

The frontend has its own gate (`cd frontend && npm run lint`) run in CI separately.

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

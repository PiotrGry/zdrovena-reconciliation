# Code Review Report - 2026-05-15

Scope: broad review of backend, frontend, and infrastructure code in the current checked-out repository state. This was not a full line-by-line audit of every file.

## Reviewed Areas

- Backend API/auth/storage: `zdrovena/api/*`, `zdrovena/common/storage.py`, `zdrovena/common/secrets.py`, Key Vault lookup path.
- Frontend runtime/auth/views: `frontend/src/auth.jsx`, main file/close/sales views, Vite and Static Web Apps config.
- Infrastructure/deploy: Terraform core files, Container App module, storage/security config, Dockerfile, and deployment workflow fragments.
- Test context: selected FastAPI, invoice router, close history, and smoke auth test files.

## Findings

### High - Container App likely cannot read Key Vault secrets

Key Vault is configured with access policies by default, but the Container App identity receives only the RBAC role `Key Vault Secrets User`. Unless RBAC authorization is explicitly enabled on the Key Vault, this role will not grant secret read access, so production secret resolution may fail.

References:

- `infra/terraform/security.tf`
- `infra/terraform/modules/container_app/main.tf`
- `zdrovena/common/secrets.py`
- `zdrovena/common/_keyvault.py`

Recommended fix: either set `enable_rbac_authorization = true` on `azurerm_key_vault`, or add an explicit Key Vault access policy for the Container App managed identity with `Get` secret permission.

### High - JWT audience validation can be silently disabled

Backend decodes JWTs with `verify_aud=false` and only performs manual audience validation if `AZURE_API_AUDIENCE` or fallback `AZURE_CLIENT_ID` is non-empty. Terraform allows `azure_client_id_entra = ""`, and the template documents this as allowed during setup. In a misconfigured production deployment, tokens may be accepted without verifying they are intended for this API.

References:

- `zdrovena/api/auth.py`
- `infra/terraform/variables.tf`
- `infra/terraform/terraform.tfvars.template`

Recommended fix: fail startup or fail auth in production when `AZURE_API_AUDIENCE` is empty. Keep auth-disabled behavior restricted to explicit local/dev mode only.

### High - File listing prefix is not validated

`GET /api/files?prefix=...` passes `prefix` directly to `storage.list_files(prefix)`. For `LocalStorageService`, `prefix=..` escapes the configured storage root and can list files outside the intended storage directory. Download/upload/delete validate keys, but list does not validate prefix.

References:

- `zdrovena/api/routers/files.py`
- `zdrovena/common/storage.py`

Recommended fix: centralize storage key validation and apply it to `prefix` as well as file keys. Reject empty traversal segments, `..`, absolute paths, backslashes, and decoded traversal attempts.

### Medium - JWT issuer is not validated

JWT validation currently verifies signature and expiry, then manually checks audience when configured. It does not validate the `iss` claim against the configured tenant. Issuer validation should be explicit for Entra ID tokens.

Reference:

- `zdrovena/api/auth.py`

Recommended fix: validate `iss` against the expected Entra issuer for `AZURE_TENANT_ID`, accounting for the token version expected by the app.

### Medium - Sales frontend can render `NaN zl`

Sales UI calls `parseFloat()` on nullable amount fields and passes the result to `fmtPLN`. `fmtPLN` only checks `null`, not `NaN`, so missing amount fields can render as `NaN zl`.

References:

- `frontend/src/views/SalesView.jsx`
- `frontend/src/data.js`

Recommended fix: make `fmtPLN` return `"-"` for non-finite values, or normalize invoice amount fields before rendering.

## Validation Performed

- `frontend`: `npm run lint` passed.
- `frontend`: `npm run build` passed.
- `backend`: `pytest` was not available on PATH.
- `backend`: `python3 -m pytest tests/test_fastapi.py tests/test_invoices_router.py tests/test_close_history.py` started under the system Python environment but hung on the first FastAPI test; it was stopped. No lingering pytest process was found afterwards.

## Notes

- The working tree had no local diff at the time of review, so this review targeted the current checked-out code rather than a specific uncommitted change set.
- `infra/terraform/terraform.tfvars` exists locally but is gitignored, so it was treated as local environment state rather than reviewed source.
- The review did not fully inspect all CLI modules, all `month_closing` internals, every test file, every CI/smoke script, or CSS line-by-line.

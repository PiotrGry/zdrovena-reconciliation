#!/usr/bin/env bash
set -euo pipefail

BRANCH_NAME="${1:-chore/spec-driven-bootstrap}"

# Helpers
info() { printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }

ensure_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    warn "File exists, skipping: $path"
  else
    mkdir -p "$(dirname "$path")"
    : > "$path"
    ok "Created: $path"
  fi
}

write_if_empty() {
  local path="$1"
  local content="$2"
  if [[ ! -f "$path" ]]; then
    mkdir -p "$(dirname "$path")"
    printf "%s" "$content" > "$path"
    ok "Created and wrote: $path"
    return
  fi

  if [[ ! -s "$path" ]]; then
    printf "%s" "$content" > "$path"
    ok "Wrote template into empty file: $path"
  else
    warn "File not empty, leaving as-is: $path"
  fi
}

# 1) Git branch (optional)
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  CURRENT_BRANCH="$(git branch --show-current || true)"
  info "Git repo detected. Current branch: ${CURRENT_BRANCH:-unknown}"

  if git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
    warn "Branch already exists: $BRANCH_NAME"
    git checkout "$BRANCH_NAME"
    ok "Checked out: $BRANCH_NAME"
  else
    git checkout -b "$BRANCH_NAME"
    ok "Created and checked out: $BRANCH_NAME"
  fi
else
  warn "Not a git repository (or git not available). Skipping branch creation."
fi

# 2) Create docs structure
info "Creating docs structure..."
mkdir -p docs/ADR scripts
ok "Ensured directories: docs/ADR, scripts"

# 3) Create docs files with minimal templates (only if empty/non-existing)
write_if_empty "docs/SPEC.md" \
"# SPEC

## Cel
(Opisz w 1–2 zdaniach po co jest ten projekt.)

## Zakres
- In scope:
- Out of scope:

## Interfejsy
- CLI:
- Integracje:

## Wymagania niefunkcjonalne
- Testy
- Logowanie
- Bezpieczeństwo

## Definition of Done
- [ ] Testy przechodzą
- [ ] Zaktualizowany RUNBOOK
- [ ] Zgodność z ustalonymi zasadami
"

write_if_empty "docs/PLAN.md" \
"# PLAN

## Backlog (checklist)
- [ ] Etap 1: ...
- [ ] Etap 2: ...
- [ ] Etap 3: ...

## Ryzyka / zależności
- ...
"

write_if_empty "docs/AGENTS.md" \
"# AGENTS

## Zasady globalne
- Zawsze przeczytaj: docs/SPEC.md i docs/PLAN.md zanim zaczniesz zmiany.
- Najpierw zaproponuj plan (krótki), potem implementuj.
- Każda zmiana funkcjonalna = testy.
- Nie dotykaj sekretów/kluczy. Nie commituj danych wrażliwych.

## Routing ról (logicznie)
- Orchestrator: plan, podział na kroki, decyzje arch.
- Backend: implementacja logiki + testy.
- QA: edge cases, pokrycie testów, regresja.
- DevOps: CI, skrypty, uruchamianie, docker.
- Security: zasady sekretów, minimalne uprawnienia, skanowanie.
"

write_if_empty "docs/RUNBOOK.md" \
"# RUNBOOK

## Uruchomienie lokalne
1. ...
2. ...

## Testy
- ./scripts/check.sh

## Typowe problemy
- ...
"

# ADR directory already created; add a starter ADR file if none exists
if compgen -G "docs/ADR/*.md" >/dev/null; then
  warn "ADR files already exist, skipping starter ADR."
else
  write_if_empty "docs/ADR/0001-module-boundaries.md" \
"# ADR 0001: Granice modułów

## Decyzja
Utrzymujemy wyraźne granice modułów: każdy moduł ma jeden publiczny entrypoint (np. orchestrator/service),
a wspólne elementy trafiają do wspólnego pakietu (np. common).

## Konsekwencje
- łatwiejsze testowanie
- mniej zależności krzyżowych
- łatwiejsze skalowanie repo
"
fi

# 4) Create scripts/check.sh
if [[ -f "scripts/check.sh" ]]; then
  warn "File exists, skipping: scripts/check.sh"
else
  cat > "scripts/check.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "[check] Running quality gate..."

# Optional: ruff if available
if command -v ruff >/dev/null 2>&1; then
  echo "[check] ruff check ."
  ruff check .
else
  echo "[check] ruff not found (skipping lint). Install ruff if you want linting."
fi

# Run tests (pytest)
if command -v pytest >/dev/null 2>&1; then
  echo "[check] pytest"
  pytest -q
else
  echo "[check] pytest not found. Install it or adjust scripts/check.sh."
  exit 1
fi

echo "[check] OK"
EOF
  chmod +x scripts/check.sh
  ok "Created: scripts/check.sh (executable)"
fi

info "Done. Next steps:"
echo "  - Fill docs/SPEC.md and docs/PLAN.md"
echo "  - Run: ./scripts/check.sh"
echo "  - Commit changes when ready."#!/usr/bin/env bash
set -euo pipefail

BRANCH_NAME="${1:-chore/spec-driven-bootstrap}"

# Helpers
info() { printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }

ensure_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    warn "File exists, skipping: $path"
  else
    mkdir -p "$(dirname "$path")"
    : > "$path"
    ok "Created: $path"
  fi
}

write_if_empty() {
  local path="$1"
  local content="$2"
  if [[ ! -f "$path" ]]; then
    mkdir -p "$(dirname "$path")"
    printf "%s" "$content" > "$path"
    ok "Created and wrote: $path"
    return
  fi

  if [[ ! -s "$path" ]]; then
    printf "%s" "$content" > "$path"
    ok "Wrote template into empty file: $path"
  else
    warn "File not empty, leaving as-is: $path"
  fi
}

# 1) Git branch (optional)
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  CURRENT_BRANCH="$(git branch --show-current || true)"
  info "Git repo detected. Current branch: ${CURRENT_BRANCH:-unknown}"

  if git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
    warn "Branch already exists: $BRANCH_NAME"
    git checkout "$BRANCH_NAME"
    ok "Checked out: $BRANCH_NAME"
  else
    git checkout -b "$BRANCH_NAME"
    ok "Created and checked out: $BRANCH_NAME"
  fi
else
  warn "Not a git repository (or git not available). Skipping branch creation."
fi

# 2) Create docs structure
info "Creating docs structure..."
mkdir -p docs/ADR scripts
ok "Ensured directories: docs/ADR, scripts"

# 3) Create docs files with minimal templates (only if empty/non-existing)
write_if_empty "docs/SPEC.md" \
"# SPEC

## Cel
(Opisz w 1–2 zdaniach po co jest ten projekt.)

## Zakres
- In scope:
- Out of scope:

## Interfejsy
- CLI:
- Integracje:

## Wymagania niefunkcjonalne
- Testy
- Logowanie
- Bezpieczeństwo

## Definition of Done
- [ ] Testy przechodzą
- [ ] Zaktualizowany RUNBOOK
- [ ] Zgodność z ustalonymi zasadami
"

write_if_empty "docs/PLAN.md" \
"# PLAN

## Backlog (checklist)
- [ ] Etap 1: ...
- [ ] Etap 2: ...
- [ ] Etap 3: ...

## Ryzyka / zależności
- ...
"

write_if_empty "docs/AGENTS.md" \
"# AGENTS

## Zasady globalne
- Zawsze przeczytaj: docs/SPEC.md i docs/PLAN.md zanim zaczniesz zmiany.
- Najpierw zaproponuj plan (krótki), potem implementuj.
- Każda zmiana funkcjonalna = testy.
- Nie dotykaj sekretów/kluczy. Nie commituj danych wrażliwych.

## Routing ról (logicznie)
- Orchestrator: plan, podział na kroki, decyzje arch.
- Backend: implementacja logiki + testy.
- QA: edge cases, pokrycie testów, regresja.
- DevOps: CI, skrypty, uruchamianie, docker.
- Security: zasady sekretów, minimalne uprawnienia, skanowanie.
"

write_if_empty "docs/RUNBOOK.md" \
"# RUNBOOK

## Uruchomienie lokalne
1. ...
2. ...

## Testy
- ./scripts/check.sh

## Typowe problemy
- ...
"

# ADR directory already created; add a starter ADR file if none exists
if compgen -G "docs/ADR/*.md" >/dev/null; then
  warn "ADR files already exist, skipping starter ADR."
else
  write_if_empty "docs/ADR/0001-module-boundaries.md" \
"# ADR 0001: Granice modułów

## Decyzja
Utrzymujemy wyraźne granice modułów: każdy moduł ma jeden publiczny entrypoint (np. orchestrator/service),
a wspólne elementy trafiają do wspólnego pakietu (np. common).

## Konsekwencje
- łatwiejsze testowanie
- mniej zależności krzyżowych
- łatwiejsze skalowanie repo
"
fi

# 4) Create scripts/check.sh
if [[ -f "scripts/check.sh" ]]; then
  warn "File exists, skipping: scripts/check.sh"
else
  cat > "scripts/check.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "[check] Running quality gate..."

# Optional: ruff if available
if command -v ruff >/dev/null 2>&1; then
  echo "[check] ruff check ."
  ruff check .
else
  echo "[check] ruff not found (skipping lint). Install ruff if you want linting."
fi

# Run tests (pytest)
if command -v pytest >/dev/null 2>&1; then
  echo "[check] pytest"
  pytest -q
else
  echo "[check] pytest not found. Install it or adjust scripts/check.sh."
  exit 1
fi

echo "[check] OK"
EOF
  chmod +x scripts/check.sh
  ok "Created: scripts/check.sh (executable)"
fi

info "Done. Next steps:"
echo "  - Fill docs/SPEC.md and docs/PLAN.md"
echo "  - Run: ./scripts/check.sh"
echo "  - Commit changes when ready."

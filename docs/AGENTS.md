# AGENTS

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

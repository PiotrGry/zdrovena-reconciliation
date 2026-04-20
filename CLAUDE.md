# CLAUDE.md

## Generalna zasada — obowiązuje wszystkich agentów AI

> **Jakość i bezpieczeństwo nad tempo.**
>
> Lepiej zgłosić problem i poczekać na decyzję właściciela, niż szybko "naprawić" CI
> przez obniżenie standardów. Zielony pipeline z obniżonymi progami to fałszywe poczucie
> bezpieczeństwa — gorsze niż czerwony pipeline z uczciwą informacją o problemie.

## gstack

- Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.
- Available skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health

## Zasady jakości kodu — dla agentów AI

### Nigdy nie rób tych rzeczy bez jawnej zgody właściciela:
- **Nie obniżaj progów jakości** (coverage, lint severity, type strictness) by "naprawić" failing CI. Zamiast tego zgłoś problem i zaproponuj dwie opcje: (a) napisanie brakujących testów, (b) wykluczenie pliku z pomiaru z komentarzem dlaczego.
- **Nie dodawaj `# type: ignore`, `# noqa`, `# pragma: no cover`** bez wyjaśnienia w commit message dlaczego jest to uzasadnione technicznie (np. third-party library bez typów, conditional import).
- **Nie skipuj kroków CI** (np. `continue-on-error: true`, `if: false`) żeby run był zielony.
- **Nie zamieniaj strict checków na warn-only** (np. bandit `-l` zamiast `-ll`, pyright `basic` zamiast `strict`) bez uzgodnienia.

### Zamiast tego:
- Jeśli coverage jest niska bo moduł jest nieprzetestowalny jednostkowo (integracja zewnętrzna) → zaproponuj `omit` dla tego pliku + uzasadnienie w PR
- Jeśli Pyright zgłasza błąd w third-party lib bez typów → `# type: ignore[import-untyped]` jest OK, ale musi być z komentarzem
- Jeśli test jest flakey → napraw przyczynę, nie skipuj testu
- Jeśli CI jest wolne → zoptymalizuj cache/concurrency, nie pomijaj kroków

### Obecny stan świadomego długu technicznego:
- `coverage --cov-fail-under=80` — próg ustawiony po sesji poprawy pokrycia. Moduły integracyjne (zoho_mail, ksef, canva, fakturownia_reports, report_downloader, invoice_date_check) są w `omit` bo wymagają live credentials/Playwright. Pokrycie mierzalne kodu biznesowego: 82%.
- `ksef.py` / `fakturownia_reports.py` — `# type: ignore` na conditional imports z try/except (lxml, signxml). Uzasadnione: Pyright nie śledzi symboli przez granicę try/except.

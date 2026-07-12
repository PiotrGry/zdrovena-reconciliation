# Zdrovena implementation plan package

Recommended repository layout:

```text
docs/
  ZDROVENA_IMPLEMENTATION_PLAN_V2.pdf
  ZDROVENA_IMPLEMENTATION_PLAN_V2.md
  implementation/issues/*.md
.github/
  copilot-instructions.md
scripts/
  create-github-issues.sh
```

Create issues:

```bash
gh auth login
./scripts/create-github-issues.sh PiotrGry/zdrovena-reconciliation
```

The GitHub integration used to prepare this package had read-only permissions and returned HTTP 403 for issue creation, so the script must run under the repository owner's GitHub session.

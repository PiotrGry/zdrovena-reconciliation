# Poświadczenia OIDC dla GitHub Actions

**Zmiana `environment:` w workflow zmienia subject claim OIDC. Jeśli dla nowego
subjectu nie istnieje poświadczenie federacyjne, job przestaje się uwierzytelniać.**

Tak wywróciło się `terraform apply` na `main` 29.08.2026: dodanie
`environment: production-infra` (#138) przestawiło subject na taki, dla którego
nic nie było zarejestrowane. Błąd wygląda tak:

```
AADSTS700213: No matching federated identity record found for presented
assertion subject 'repo:PiotrGry/zdrovena-reconciliation:environment:production-infra'
```

## Która tożsamość naprawdę działa

Istnieją **dwa** obiekty o nazwie `zdrovena-github-actions` i to jest główne
źródło pomyłek:

| Obiekt | ClientId | Rola na subskrypcji | Zarządzany przez |
| --- | --- | --- | --- |
| **App registration** (Entra) | `a530cb7f-6948-42cd-a31c-1ceddab3ce2a` | **Contributor** | ręcznie |
| Managed identity | `4b002476-c9f4-42be-83b1-d12281164141` | **brak** | Terraform (`security.tf`) |

`AZURE_CLIENT_ID` wskazuje **app registration**. Managed identity nie ma żadnych
przypisań ról — nawet uwierzytelniona nic by nie zrobiła.

Sprawdzenie, gdyby kiedyś było wątpliwe: `_deploy.yml` działa z
`environment: production`, a poświadczenie dla tego subjectu ma wyłącznie app
registration.

## Dlaczego to nie jest w Terraformie

Świadomie. Wciągnięcie app registration do stacku wymagałoby providera `azuread`
i nadania tożsamości CI uprawnień do zarządzania aplikacjami w Entra — czyli
pozwolenia jej zarządzać samą sobą. To eskalacja uprawnień, a przy okazji
zależność bootstrapowa: pomyłka w imporcie kasuje poświadczenia i zamyka dostęp
do CI, którego nie da się już naprawić Terraformem, bo Terraform właśnie stracił
tożsamość.

Bootstrap tożsamości trzyma się poza stackiem, który ona aplikuje — z tego samego
powodu, dla którego backend stanu tworzy się ręcznie.

## Zarejestrowane subjecty

Stan na 30.08.2026, app registration `a530cb7f-…`:

| Nazwa | Subject |
| --- | --- |
| `github-develop` | `repo:PiotrGry/zdrovena-reconciliation:ref:refs/heads/develop` |
| `github-main` | `repo:PiotrGry/zdrovena-reconciliation:ref:refs/heads/main` |
| `github-staging` | `repo:PiotrGry/zdrovena-reconciliation:environment:staging` |
| `github-production` | `repo:PiotrGry/zdrovena-reconciliation:environment:production` |

Aktualna lista:

```
az ad app federated-credential list \
  --id a530cb7f-6948-42cd-a31c-1ceddab3ce2a \
  --query "[].{name:name, subject:subject}" -o table
```

## Zanim zmienisz `environment:` w workflow

1. Sprawdź, czy subject dla nowego środowiska jest na liście wyżej.
2. Jeśli nie ma — **najpierw dodaj poświadczenie**, potem scal zmianę workflow.

```
az ad app federated-credential create \
  --id a530cb7f-6948-42cd-a31c-1ceddab3ce2a \
  --parameters '{
    "name": "github-<środowisko>",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:PiotrGry/zdrovena-reconciliation:environment:<środowisko>",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

Cofnięcie:

```
az ad app federated-credential delete \
  --id a530cb7f-6948-42cd-a31c-1ceddab3ce2a \
  --federated-credential-id github-<środowisko>
```

## Zmienne środowiskowe idą w parze

Job związany ze środowiskiem widzi **wyłącznie** zmienne i sekrety tego
środowiska. Przy przenoszeniu joba między środowiskami trzeba przenieść też
zmienne — inaczej OIDC przejdzie, a job padnie linijkę dalej na fail-fast.

Dokładnie to stało się przy #365: środowisko zmieniono na `production`,
a `OPS_ALERT_EMAIL` była tylko na `staging` i `production-infra`.

```
gh variable list --env production
```

Dziś `OPS_ALERT_EMAIL` jest ustawiona na `staging`, `production`
i `production-infra`.

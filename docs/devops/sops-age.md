# SOPS + age

This repo stores private runtime credentials in Azure Key Vault. Use SOPS + age only for GitOps-managed Kubernetes Secrets or YAML/JSON config that must live encrypted in Git.

## Jak To Ma Dzialac

Model jest prosty:

- klucz publiczny age (`age1...`) sluzy do szyfrowania i moze byc zapisany w repo, np. w `.sops.yaml`;
- klucz prywatny age (`AGE-SECRET-KEY-...`) sluzy do odszyfrowania i nigdy nie trafia do Gita;
- do repo commitujemy tylko zaszyfrowane pliki `*.sops.yaml` / `*.sops.json`;
- lokalnie SOPS uzywa prywatnego klucza z `~/.config/sops/age/keys.txt`;
- w klastrze GitOps controller, np. Flux, dostaje prywatny klucz jako Kubernetes Secret i odszyfrowuje manifesty przed zastosowaniem.

### 1. Wygeneruj Klucz

```bash
scripts/sops-age-bootstrap.sh dev
```

Skrypt utworzy plik `age-dev.agekey` i wypisze publiczny klucz `age1...`.

Prywatny plik `age-dev.agekey`:

- skopiuj do menedzera sekretow zespolu;
- trzymaj lokalnie tylko wtedy, gdy potrzebujesz odszyfrowywac pliki;
- nie commituj do Gita.

Publiczny klucz `age1...`:

- wpisz do `.sops.yaml`;
- moze byc widoczny w repo.

### 2. Utworz Konfiguracje Repo

```bash
cp .sops.yaml.example .sops.yaml
```

W `.sops.yaml` podmien placeholder:

```yaml
age: age1REPLACE_WITH_DEV_PUBLIC_KEY
```

na realny publiczny klucz:

```yaml
age: age1abc...
```

Na start mozna uzyc jednego klucza dla `dev`, `staging` i `prod`, ale docelowo produkcja powinna miec osobny klucz prywatny z ograniczonym dostepem.

### 3. Sprawdz Lokalny Klucz Prywatny

SOPS domyslnie szuka kluczy tutaj:

```bash
~/.config/sops/age/keys.txt
```

Sprawdz:

```bash
ls -l ~/.config/sops/age/keys.txt
```

Uprawnienia powinny byc ograniczone:

```bash
chmod 600 ~/.config/sops/age/keys.txt
```

### 4. Utworz Sekret Kubernetes

Przyklad dla dev:

```bash
mkdir -p clusters/dev/secrets

kubectl create secret generic db-creds \
  --from-literal=password='super-tajne-haslo' \
  --dry-run=client -o yaml \
  > clusters/dev/secrets/db.sops.yaml
```

### 5. Zaszyfruj Plik

```bash
sops -e -i clusters/dev/secrets/db.sops.yaml
```

Po szyfrowaniu plik powinien zawierac:

- wartosci `ENC[...]`;
- sekcje `sops:`.

### 6. Edytuj Sekrety Tylko Przez SOPS

```bash
sops clusters/dev/secrets/db.sops.yaml
```

SOPS tymczasowo odszyfruje plik w edytorze, a po zapisie znowu go zaszyfruje.

Podglad odszyfrowanej wersji:

```bash
sops -d clusters/dev/secrets/db.sops.yaml
```

### 7. Sprawdz Przed Commitem

```bash
scripts/check-sops-secrets.sh
```

Ten check blokuje:

- prywatne pliki age w Git;
- tekst `AGE-SECRET-KEY-*` w sledzonych plikach;
- pliki `*.sops.yaml` / `*.sops.json`, ktore nie maja `sops:` i `ENC[...]`.

### 8. Co Commitowac

Do Gita moga trafic:

- `.sops.yaml`;
- `clusters/dev/secrets/db.sops.yaml`;
- publiczne klucze `age1...`;
- dokumentacja.

Do Gita nie moga trafic:

- `age-dev.agekey`;
- `age-staging.agekey`;
- `age-prod.agekey`;
- `~/.config/sops/age/keys.txt`;
- jakikolwiek tekst `AGE-SECRET-KEY-*`.

### 9. Jak Dziala GitOps

W repo jest zaszyfrowany manifest, np.:

```text
clusters/prod/secrets/db.sops.yaml
```

Flux pobiera go z Gita, odszyfrowuje w klastrze przy uzyciu prywatnego klucza z Kubernetes Secret i aplikuje jako normalny Kubernetes Secret.

Dla Flux konfiguracja `Kustomization` musi miec:

```yaml
decryption:
  provider: sops
  secretRef:
    name: sops-age
```

Secret z prywatnym kluczem age tworzy sie tak:

```bash
kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-file=age.agekey=age-prod.agekey
```

Ten Kubernetes Secret zawiera klucz prywatny, wiec dostep powinien miec tylko GitOps controller i operatorzy produkcyjni.

## Local Setup

Install tools:

```bash
brew install sops age
```

or on Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y age
SOPS_VER=3.9.4
curl -Lo sops "https://github.com/getsops/sops/releases/download/v${SOPS_VER}/sops-v${SOPS_VER}.linux.amd64"
sudo install -m 0755 sops /usr/local/bin/sops
```

Generate a local key for an environment:

```bash
scripts/sops-age-bootstrap.sh dev
scripts/sops-age-bootstrap.sh staging
scripts/sops-age-bootstrap.sh prod
```

Commit only public recipients (`age1...`). Never commit `AGE-SECRET-KEY-*` or `*.agekey`.

## Repository Configuration

Create the real SOPS config from the template:

```bash
cp .sops.yaml.example .sops.yaml
```

Replace every `age1REPLACE_WITH_*` value with a real public age key. Use separate recipients for `dev`, `staging`, and `prod`; production private keys should be restricted to production operators and the GitOps controller.

The default policy encrypts only Kubernetes Secret payload fields:

```yaml
encrypted_regex: '^(data|stringData)$'
```

That keeps `apiVersion`, `kind`, and `metadata` readable in diffs.

## Encrypting Files

Create Kubernetes Secret YAML without writing plaintext secrets to disk:

```bash
kubectl create secret generic db-creds \
  --from-literal=password='replace-me' \
  --dry-run=client -o yaml \
  | sops -e --encrypted-regex '^(data|stringData)$' /dev/stdin \
  > clusters/prod/secrets/db.sops.yaml
```

Encrypt an existing file using `.sops.yaml`:

```bash
sops -e -i clusters/prod/secrets/db.sops.yaml
```

Edit encrypted files through SOPS:

```bash
sops clusters/prod/secrets/db.sops.yaml
```

Decrypt for inspection:

```bash
sops -d clusters/prod/secrets/db.sops.yaml
```

## Flux CD

Flux has native SOPS support. Put the private age key into the cluster:

```bash
kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-file=age.agekey=age-prod.agekey
```

Then add decryption to the Flux `Kustomization`:

```yaml
decryption:
  provider: sops
  secretRef:
    name: sops-age
```

Limit RBAC access to this Secret to the Flux `kustomize-controller`, and enable etcd encryption at rest where possible.

## ArgoCD

ArgoCD does not decrypt SOPS natively. Use `ksops` with Kustomize plugins, or prefer an existing secret-store integration such as `argocd-vault-plugin` if the team already standardizes on Vault or Azure Key Vault.

Minimum ksops shape:

```yaml
apiVersion: viaduct.ai/v1
kind: ksops
metadata:
  name: secret-generator
files:
  - ./db.sops.yaml
```

Reference it from `kustomization.yaml`:

```yaml
generators:
  - ./secret-generator.yaml
```

Mount the private age key into `argocd-repo-server` and set `SOPS_AGE_KEY_FILE` to that mounted file.

## Checks

Run:

```bash
scripts/check-sops-secrets.sh
```

The check fails when a private age key file is tracked, `AGE-SECRET-KEY-*` appears in tracked files, or a committed `*.sops.yaml` / `*.sops.json` lacks SOPS metadata and `ENC[...]` values.

## Rotation

Rotate data keys:

```bash
sops --rotate -i clusters/prod/secrets/db.sops.yaml
```

Update recipients after changing `.sops.yaml`:

```bash
sops updatekeys -y clusters/prod/secrets/db.sops.yaml
```

For many files:

```bash
find clusters -name '*.sops.yaml' -exec sops updatekeys -y {} \;
```

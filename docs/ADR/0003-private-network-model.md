# ADR 0003: Model sieci prywatnej — Service Endpoints, nie Private Endpoints

## Date
2026-08-29

## Authors
Piotr Gryzło

## Kontekst

`enable_private_network`, komentarze w `network.tf` i faktycznie tworzone zasoby
opisywały trzy różne modele (#215). Stan zastany:

| Miejsce | Co twierdziło |
| --- | --- |
| Nagłówek `network.tf` | Service Endpoints jako **tania alternatywa** dla Private Endpoints, oszczędność €29/mies. |
| Zasoby w `network.tf` | tworzyły **również** Private Endpointy i strefy Private DNS dla Blob i Key Vault |
| Opis zmiennej | „Service Endpoints = FREE (vs €14/month for Private Endpoints)" |
| Komentarz w `storage.tf` | „Private network mode: access only via **Private Endpoint** from VNet" |
| ACL w `storage.tf` | `virtual_network_subnet_ids` — czyli mechanizm **Service Endpoint**, nie PE |
| `checkov:skip=CKV2_AZURE_33` | „Private endpoint requires VNet **not present in this architecture**" |

Do tego Key Vault **nie miał w ogóle gałęzi** dla trybu prywatnego: jego
`network_acls` to na sztywno `default_action = "Allow"`. Włączenie flagi
zbudowałoby więc Private Endpoint i strefę DNS dla vaultu, zostawiając jego
firewall całkowicie otwarty — izolacja, za którą się płaci, nie dawałaby nic.

Flaga ma domyślnie `false` i nic z tego nigdy nie zostało wdrożone, więc żaden
z tych sprzecznych opisów nie został skonfrontowany z rzeczywistością.

## Decyzja

**Service Endpoints dla Storage i Key Vault. Bez Private Endpointów.**

Zasoby `azurerm_private_endpoint`, `azurerm_private_dns_zone`,
`azurerm_private_dns_zone_virtual_network_link` oraz podsieć
`private-endpoints-subnet` zostają **usunięte**. Kod dopasowuje się do decyzji,
która była już zapisana w nagłówku pliku — a nie odwrotnie.

Key Vault dostaje tę samą warunkową ACL, co Storage: przy włączonej fladze
`default_action = "Deny"` z regułą na podsieć Container Apps.

ACR zostaje publiczny, z uwierzytelnianiem przez Managed Identity. Private
Endpoint dla ACR wymaga warstwy Premium (~€159/mies.).

## Uzasadnienie

- **Oba modele naraz to nie jest „głębsza obrona", tylko dwie ścieżki
  z osobnymi trybami awarii.** Przy Private Endpoincie ruch omija firewall
  konta, więc reguła na podsieć nic nie robi; przy Service Endpoincie to reguła
  na podsieć *jest* kontrolą. Utrzymywanie obu oznacza, że przy diagnozie
  „dlaczego to nie ma dostępu" trzeba za każdym razem ustalać, która ścieżka
  jest aktywna.
- **Granicą bezpieczeństwa jest tu tożsamość, nie pozycja w sieci.** Storage ma
  wyłączony Shared Key (`shared_access_key_enabled = false`), nie wydajemy SAS-ów,
  a dostęp idzie przez RBAC Managed Identity. Service Endpoint dokłada do tego
  ruch po szkielecie Microsoftu i firewall na podsieć.
- **Koszt.** Service Endpoints są bezpłatne; koszt trybu prywatnego to sam ruch
  VNet, ~€3/mies. Private Endpointy dla Blob i Key Vault to ~€29/mies. więcej
  za model, którego ten profil danych nie wymaga.
- **Dane są biznesowe, nie regulowane.** Nie przetwarzamy danych medycznych ani
  kartowych, więc nie ma wymogu, który wymuszałby prywatne adresy IP.

## Ograniczenia i zagrożenia

- Service Endpoint używa **publicznych adresów Azure** (20.x.x.x), a nie
  prywatnych (10.0.x.x). Ruch nie wychodzi na publiczny internet, ale nie idzie
  też Twoim prywatnym tunelem.
- Static Web App **nie może** korzystać z Service Endpointu — to usługa CDN
  i musi zostać publiczna. Nie zmienia to modelu, bo SWA nie ma dostępu do
  Storage ani Key Vaulta.
- Włączenie flagi migruje Container Apps Environment do VNetu. To **odtworzenie
  środowiska**, nie zmiana w miejscu — planuj jak przerwę w działaniu.
- Przy `default_action = "Deny"` każda ścieżka spoza podsieci przestaje działać:
  lokalne `az storage`, ręczne wgranie pliku z laptopa, dowolne narzędzie
  zewnętrzne. To jest cel, ale trzeba o tym wiedzieć przed włączeniem.

## Kiedy tę decyzję odwrócić

Jeśli pojawi się wymóg zgodności (HIPAA, PCI-DSS) albo umowny wymóg prywatnej
adresacji. Wtedy wraca: podsieć `private-endpoints-subnet` (/24), strefy
`privatelink.blob.core.windows.net` i `privatelink.vaultcore.azure.net`,
powiązania stref z VNetem oraz po jednym `azurerm_private_endpoint` na Blob
i Key Vault. Wersja przed usunięciem jest w historii gita — commit zamykający
#215. Wraz z nimi trzeba **usunąć** reguły `virtual_network_subnet_ids`, bo
przy PE są martwe i tylko mylą.

## Konsekwencje

- Nagłówek `network.tf`, opis zmiennej i komentarze w `storage.tf` opisują teraz
  to, co plik faktycznie tworzy.
- Key Vault ma domknięty tryb prywatny, którego wcześniej nie miał w ogóle.
- Włączenie sieci prywatnej pozostaje osobną, jawnie zatwierdzaną zmianą:
  flaga zostaje `false`, a `apply` na produkcji wymaga ręcznego zatwierdzenia
  planu (#138).
- RBAC, TLS 1.2 i zakaz Shared Key pozostają nietknięte — ta decyzja niczego
  z nich nie osłabia.

## Weryfikacja

`tests/test_private_network_policy.py` pilnuje, żeby kod i ta decyzja nie
rozjechały się ponownie: brak zasobów Private Endpoint, obecność Service
Endpointów, warunkowe ACL po obu stronach i nienaruszone gwarancje tożsamościowe.

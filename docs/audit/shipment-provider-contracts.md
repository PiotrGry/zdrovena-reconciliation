# Kontrakty tworzenia przesyłek — Allegro, Apaczka, InPost

Stan dokumentacji sprawdzony: 2026-08-17.

Zakres: produkcyjne ścieżki tworzenia przesyłki używane przez Zdrovena. To nie
jest opis wszystkich operacji providerów.

## Allegro — Wysyłam z Allegro

Endpoint: `POST /shipment-management/shipments/create-commands`.

Wymagania egzekwowane przez klienta i fake provider:

- nagłówki `Authorization`, `Accept` i `Content-Type`; oba nagłówki treści mają
  wartość `application/vnd.allegro.public.v1+json`,
- `input.sender`, `input.receiver`, `input.receiver.email`,
- niepusta tablica `input.packages` i `type` w każdej paczce,
- `receiver.point` warunkowo dla dostawy do punktu.

`commandId` i `deliveryMethodId` są obecnie opcjonalne po stronie Allegro.
Zdrovena nadal generuje własny `commandId`, a `deliveryMethodId` może pominąć.
`POST` jedynie przyjmuje komendę i zwraca jej identyfikator oraz input. Status
`IN_PROGRESS`/`SUCCESS`/`ERROR` pochodzi z osobnego `GET`; przy oczekiwaniu
odpowiedź zawiera `Retry-After`.
Odpowiedź `GET /shipment-management/delivery-proposals/{orderId}` zawiera dane
pod `suggestedInput.sender` i `suggestedInput.receiver` — nie pod historycznymi,
używanymi wcześniej w fake providerze polami `senderData`/`receiverData`.

Źródło: [oficjalny poradnik Wysyłam z Allegro](https://developer.allegro.pl/tutorials/jak-zarzadzac-przesylkami-przez-wysylam-z-allegro-LRVjK7K21sY).

## Apaczka API v2

Endpoint: `POST /api/v2/order_send/`.

Każdy request ma podpisaną kopertę `app_id`, `request`, `expires`, `signature`.
Emulator wylicza HMAC-SHA256 ponownie na dokładnym JSON-ie przesłanym w polu
`request`, uwzględnia końcowy slash endpointu i odrzuca podpis oraz wygasły
timestamp w takiej samej kopercie `status/message/response` jak Apaczka.
W `request.order` nasza krajowa ścieżka wymaga co najmniej:

- `service_id`,
- `address.sender` i `address.receiver` wraz z danymi kontaktowymi i adresowymi,
- niepustego `shipment` z wagą, trzema wymiarami i `shipment_type_code`,
- `pickup`,
- niepustego `content` na poziomie `order`.

Istotne: `content` nie jest polem elementu `shipment`. Oficjalny przykład
struktury umieszcza je obok `comment` i `is_zebra`, bezpośrednio w `order`.
Publiczna dokumentacja Apaczki nie oznacza wszystkich pól maszynowym znacznikiem
`required`; powyższy minimalny podzbiór jest zgodny z opublikowaną strukturą i
walidacją zwróconą przez produkcyjne API dla brakującej zawartości.

Pobranie jest przekazywane w opcjonalnym `request.order.cod`:

- `amount` jest dodatnią liczbą całkowitą w groszach, np. `20030`,
- w pierwszej wersji Zdroveny `currency` ma wartość `PLN`,
- `bankaccount` jest 26-cyfrowym polskim NRB pobieranym z sekretu
  `apaczka-cod-bank-account` (`APACZKA_COD_BANK_ACCOUNT` lokalnie).

Dokumentacja punktów Apaczki udostępnia też flagę `option_cod`, która informuje,
czy konkretny punkt obsługuje pobranie. Shopify zapisuje w drafcie identyfikator
i dane opisowe punktu wybranego przez widget Octolize, ale nie zapisuje tej flagi.
Dlatego preview i wykonanie COD pobierają właściwą listę z `points/:type`
(cache 24 godziny, zgodnie z limitem jednego pobrania na dobę) i wymagają
`option_cod === true` dla wybranego `foreign_address_id`. Brak punktu, brak flagi,
wartość `false` albo nieznany typ punktu blokują operację przed `order_send`.
Ścieżka krajowa nie wysyła też COD przy więcej niż jednej fizycznej paczce: jeden
request z pełną kwotą per paczka groziłby wielokrotnym pobraniem. Brak lub błędny
rachunek blokuje payload przed wywołaniem API.

Źródło: [oficjalna dokumentacja Web API v2 Apaczka](https://panel.apaczka.pl/dokumentacja_api_v2.php).

## InPost — legacy ShipX v1

Endpoint używany przez aplikację:
`POST /v1/organizations/{organization_id}/shipments`.

Wymagania wspólne:

- `receiver`, `parcels`, `service`,
- `receiver.phone`,
- co najmniej jedna paczka: `template` albo komplet `dimensions` + `weight`,
- wymiary i waga mają wartości co najmniej 1.

Warunki zależne od usługi:

- Paczkomat: `receiver.email` oraz `custom_attributes.target_point`,
- kurier: odbiorca ma `company_name` albo `first_name` + `last_name`, a także
  `receiver.address` z ulicą, numerem budynku, miastem i kodem pocztowym,
- `sender` jest opcjonalny; gdy go przekazujemy dla kuriera, dane adresowe są w
  zagnieżdżonym `sender.address`, a `sender.email` i `sender.phone` są wymagane.

`reference` jest opcjonalne w ShipX i jest wyłącznie identyfikatorem biznesowym.
Nie jest udokumentowanym kluczem idempotencji: ponowienie `POST` może utworzyć
drugą płatną przesyłkę. Ochronę przed duplikatem zapewnia stan draftu w
Zdrovenie, nie fake provider.

Utworzenie jest asynchroniczne. `POST` zwraca `status=created` i może zwrócić
`tracking_number=null`; dopiero późniejszy `GET /v1/shipments/{id}` przechodzi
do `confirmed` i udostępnia tracking. Zdrovena zapisuje ID z pierwszej
odpowiedzi, polluje ten sam zasób i przy retry nie wykonuje kolejnego `POST`.

Pobranie dla przesyłek krajowych jest opcjonalnym obiektem `cod`, np.
`{"amount": 200.30, "currency": "PLN"}`. Dotyczy obsługiwanych usług
paczkomatowych i kurierskich; dostępność oraz limity nadal zależą od usługi i
umowy organizacji, więc odpowiedź walidacyjna ShipX pozostaje źródłem prawdy.
`cod` nie jest elementem `additional_services`. Tak samo jak dla Apaczki,
Zdrovena v1 blokuje COD przy więcej niż jednej fizycznej paczce.

Źródła: [tworzenie przesyłki w trybie uproszczonym](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/28639258/1.23.0%2BShipment%2Bcreation%2Bin%2Bsimplified%2Bmode),
[usługi dodatkowe i COD](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/28639264).

## Shopify → COD — reguła mapowania

Zamówienie Shopify `#1706` potwierdziło produkcyjny kształt danych bez potrzeby
ujawniania danych odbiorcy:

| Pole Shopify | Wartość / znaczenie |
|---|---|
| `payment_gateway_names` | `["Cash on Delivery (COD)"]` — jedyny marker COD |
| `financial_status` | `pending` — informacja pomocnicza, niewystarczająca do wykrycia COD |
| `total_outstanding` | `200.30` — kwota pobrania |
| `currency` | `PLN` |

Detekcja porównuje dokładną nazwę bramki bez rozróżniania wielkości liter; nie
zgaduje COD na podstawie samego statusu `pending`. Draft przechowuje kwotę jako
tekst dziesiętny z dwoma miejscami, aby uniknąć błędów zmiennoprzecinkowych.
Brak kwoty, niepoprawna precyzja, nieobsługiwana waluta lub więcej niż jedna
paczka kierują draft do `needs_review`. Podgląd realizacji zawiera COD, więc
zmiana kwoty unieważnia fingerprint i wymaga ponownego potwierdzenia operatora.

## Dlaczego wcześniejsze testy przepuszczały błędne payloady

Poprzednie fake providery sprawdzały głównie pola najwyższego poziomu. Nie weryfikowały
vendorowego `Content-Type` Allegro, rootowego `order.content` Apaczki ani pól
warunkowych i zagnieżdżonego adresu InPost. Dodatkowo fake Allegro sam zwracał
nieudokumentowany kształt proposal, więc test i implementacja potwierdzały ten
sam błędny model.

Obecne emulatory są rozdzielone per provider. Każdy ma własną walidację,
autoryzację, format błędów i lifecycle; nie importują builderów ani modeli
klientów Zdroveny. Test integracyjny dopiero w drugiej kolejności uruchamia
klienta aplikacji przeciwko temu niezależnemu kontraktowi.

## Idempotencja po błędzie

Poller Allegro identyfikuje oryginalny draft przez parę
`(source="allegro", external_order_id)` niezależnie od tego, czy ma status
`error`. Synchronizacja może odświeżyć dane zamówienia, ale zachowuje `id`,
status i komunikat błędu. Dzięki temu ręczny retry działa na tym samym rekordzie,
a kolejny przebieg pollera nie tworzy duplikatu. Rekordy oznaczone jawnie jako
`is_replacement` pozostają poza tą deduplikacją.

# Niejednoznaczna wysyłka maila — procedura

Dotyczy maili do klienta w module Uszkodzenia i paczki do księgowej
w Zamknięciu miesiąca. Oba używają tej samej semantyki
(`zdrovena/common/send_attempt.py`).

## Dlaczego taki stan w ogóle istnieje

Blokada współbieżności („claim") powstrzymuje dwa kliknięcia przed dwiema
wysyłkami. Nie domyka natomiast okna między „SMTP przyjął wiadomość"
a „zdążyliśmy to zapisać":

1. blokada wzięta, oznaczone jako wysyłanie,
2. SMTP przyjmuje wiadomość,
3. proces albo kontener pada przed zapisem,
4. blokada wygasa i wygląda na porzuconą,
5. kolejna próba wysyła **drugiego** maila.

Krok 4 był tym, co zamieniało awarię w duplikat: blokada wygasająca po cichu
zakłada, że nic się nie wydarzyło — a po kroku 2 to założenie jest fałszywe.

Dlatego porzucona próba nie zwalnia się sama. Przechodzi w stan `unknown`,
który **blokuje** wysyłkę i czeka na człowieka. Z naszej strony nikt nie umie
odróżnić „przyjęte i zgubione" od „nigdy nie wysłane" — wie o tym tylko skrzynka
odbiorcy. Bezpieczne jest zapytać, nie zgadywać; zgadywanie kosztuje klienta
duplikat wiadomości.

## Stany

| Stan | Znaczenie | Czy blokuje wysyłkę |
| --- | --- | --- |
| brak | nic nie próbowano | nie |
| `pending` | zapisane przed kontaktem z SMTP, wysyłka trwa | tak |
| `unknown` | `pending` starszy niż 10 minut — proces nie żyje | **tak, do decyzji operatora** |
| `confirmed` | SMTP przyjął i zapisaliśmy to | tak |
| `failed` | SMTP **odmówił** — nic nie poleciało | nie, ponowienie jest bezpieczne |

`failed` zapisujemy wyłącznie przy odmowie, którą faktycznie zobaczyliśmy
(`SMTPResponseException`). Timeout albo zerwane połączenie zostawiają `pending`,
bo wiadomość mogła zostać przyjęta — zapisanie „porażki" zaprosiłoby automatyczne
ponowienie.

## Co zrobić, gdy widzisz `unknown`

1. **Sprawdź, czy mail poszedł.** Zoho Mail → Wysłane, filtruj po adresacie
   i temacie z okna czasowego próby (`started_at`).
2. **Rozstrzygnij próbę.**

   Uszkodzenia:
   ```
   POST /api/damage-cases/{case_id}/resolve-email-attempt
   {"delivered": true,  "note": "widzę w Wysłanych"}
   {"delivered": false, "note": "brak w Wysłanych"}
   ```

   Zamknięcie miesiąca:
   ```
   POST /api/close/workflow/email-attempt
   {"year": 2026, "month": 6, "delivered": true, "note": "..."}
   ```

3. `delivered: true` zamyka sprawę **bez drugiej wiadomości**.
   `delivered: false` odblokowuje ponowienie.

Decyzja jest przypisana do operatora (`resolved_by`) i trafia do zdarzeń
`damage.email_attempt_resolved` / `close.email_attempt_resolved`.

## Czego nie zapisujemy

Odcisk (`fingerprint`) to SHA-256 z adresatów, tematu i referencji artefaktu.
Sam adres, temat ani treść nie trafiają do rekordu próby — wystarczy, że da się
zauważyć, iż wznawiana próba nie jest tą, którą zaczęto.

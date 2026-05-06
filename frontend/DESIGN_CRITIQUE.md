# Zdrovena — krytyka designu frontendu + plan zmian

**Stack:** Vite + React 18, MSAL, czyste CSS (zero UI libki), 3 fonty (Inter / Fraunces / JetBrains Mono).
**Audytowane:** `src/App.jsx`, `src/main.jsx`, `index.html`, `src/styles/index.css`, 9 widoków, 5 komponentów, `lang.js`, `data.js`, `auth.jsx`, `features.js`.
**Kontekst:** wewnętrzny panel B2B do reconciliation (faktury, JPK, KSeF, zamknięcie miesiąca). Faza polish/refinement — apka działa, ale brak spójnego systemu i dyscypliny stylowej.

---

## TL;DR

Apka wygląda **lepiej niż przeciętna apka pisana bez designera** — masz tokeny CSS, sensowną typografię (Inter + Fraunces serif do nagłówków to świadoma decyzja), spójną kolorystykę teal+brąz, density-aware layout. To jest powyżej średniej.

Ale **fundament się rozjeżdża** w trzech miejscach:

1. **Tokeny w :root nie są używane konsekwentnie** — w komponentach JSX jest 30+ odwołań do nieistniejących tokenów (`--err`, `--ok`, `--bg-2`, `--ok-bg`), które fall-backują na hardkodowane hexy. Skutek: kolory rozjeżdżają się między CSS-em a JSX-em (np. czerwień błędu w SalesView jest `#e53e3e` zamiast tokenowego `#c42b1c`).
2. **128 inline `style={{...}}`** rozproszonych po 10 plikach — to anty-design-system. Każdy widok wymyśla na nowo padding, gap, font-size. Stąd wrażenie chaosu.
3. **A11y jest zepsute w kilku krytycznych miejscach** — modal bez focus trapu, dropdown na `<div>`, tabele bez `aria-sort`, toast bez `aria-live`. Osoby na klawiaturze i screen readerach nie obsłużą połowy apki.

Jeden weekend porządnej dyscypliny → poziom Linear/Notion. Bez porządkowania → osuwa się dalej.

---

## Co działa dobrze (zachować)

- **System tokenów** — 24 zmienne CSS pokrywają kolory, radii, cienie, spacing, fonty, density. Solidny fundament.
- **Density variants** (`data-density="roomy/cozy/compact"`) — przemyślane, mało kto to robi w hobbystycznych apkach.
- **Typografia mieszana** — Inter (UI) + Fraunces (h1, kpi-value, modal-title) daje produktowi charakter. JetBrains Mono dla danych tabularnych to klasyk.
- **Kolory** — paleta teal `#1a5f7a` + akcent brąz `#7a4f1a` na bardzo lekko ciepłym tle `#fafaf8` jest oryginalna i nie wygląda jak Tailwind default.
- **Pill, status badge, ext-chip** — sensowne wzorce, używane konsekwentnie w tabelach.
- **Form `:focus-within` + 3px ring na primary-50** — to jest detal z dojrzałych systemów.
- **Spinner + animacje (rise, pulse, fade)** — łagodne, krótkie czasy (.12-.25s), bez disco.
- **Tabele z `font-variant-numeric: tabular-nums`** — kwoty się ładnie wyrównują, mało kto pamięta.
- **Sidebar nav-group struktura** + footer ze statusem zdrowia — czytelne IA.

---

## P0 — KRYTYCZNE (rozjazdy spójności i blokery a11y)

### P0-1. Zepsute referencje do tokenów — silent color drift

**Problem:** w JSX-ach jest sześć tokenów których nie ma w `:root`. CSS fall-backuje na drugą wartość, czyli na hardcoded hex/keyword — i dlatego "ten sam czerwony" w różnych miejscach jest INNYM czerwonym.

| Token używany | Co istnieje w `:root` | Skutek |
|---|---|---|
| `var(--err, #e53e3e)` | `--error: #c42b1c` | dwa różne czerwone, jeden brand-czerwony, drugi generic |
| `var(--ok, #38a169)` | `--success: #2d7a4f` | dwa różne zielone |
| `var(--bg-2)` | brak | przezroczyste tło |
| `var(--ok-bg, #f0fdf4)` | brak | sukcesowy banner ma kolor spoza palety |
| `var(--warning, orange)` | `--warning: #c47a1a` | fallback to keyword `orange` (#FFA500) — jaskrawy! |

**Konkretne miejsca:**
- `src/views/SalesView.jsx:97` → `color: 'var(--err, #e53e3e)'`
- `src/views/CloseView.jsx:99,193,203,478,494,501,615` → `--err`, `--ok`, `--bg-2`, `--ok-bg`, `--warning, orange`
- `src/views/CloseView.jsx:625` → `color: incomplete ? 'var(--warning, orange)' : 'var(--text-2)'`

**Fix:** globalny find/replace:
```
--err   →  --error
--ok    →  --success
--bg-2  →  --surface-2
--ok-bg →  dodaj nowy token --success-50 do :root i użyj go
--warning, orange → --warning
```

Albo lepiej: **usuń wszystkie fallbacki** (`var(--token, FALLBACK)` formą zwykłą `var(--token)`) — dzięki temu nieistniejące tokeny się WYPALĄ wizualnie i wyłapiesz problem od razu zamiast ciszą.

### P0-2. 128 inline `style={{...}}` — anty-design-system

**Liczby:**
- `CloseView.jsx`: **57** inline styli (najgorsze)
- `SettingsView.jsx`: 14
- `FilesView.jsx`: 13
- `UsersView.jsx`: 8
- `SalesView.jsx`: 15
- ... łącznie 128 w 10 plikach

**Dlaczego to boli:** kiedy zmienisz `--gap` z 18px na 16px globalnie, połowa apki zareaguje, druga nie (bo ma `gap: 24` zahardkodowane w JSX). Tabele i karty tracą wspólny rytm.

**Przykłady patologii:**
```jsx
// CloseView.jsx:444  — sticky panel z hardkodowanym wszystkim
style={{ position: 'sticky', top: 0, zIndex: 10, background: 'var(--surface)',
         borderBottom: '1px solid var(--border)', padding: '14px 26px' }}

// CloseView.jsx:450  — pole select z hardkodowaną typografią
style={{ flex: 1, fontSize: 16, fontWeight: 600, padding: '6px 10px',
         border: '1.5px solid var(--border)', borderRadius: 7, ... }}

// SettingsView.jsx:39  — grid template
style={{ display: 'grid', gridTemplateColumns: '140px 1fr', rowGap: 10, ... }}
```

**Fix (etap 2 planu):** dla każdego inline style klasy pasującej do wzorca → klasa CSS w `index.css`:
- `.sticky-bar` zamiast tych 5 inline styli sticky panelu
- `.modal-month-select` zamiast hardkodowania selectów
- `.account-grid` zamiast `gridTemplateColumns: '140px 1fr'`

Zostawić inline TYLKO dla naprawdę dynamicznych wartości (np. `--kpi-accent` ustawiane CSS variable per KPI).

### P0-3. Modal bez podstaw a11y

**`src/views/CloseView.jsx:434`** — `<div className="modal-backdrop open"><div className="modal">` :
- brak `role="dialog"` + `aria-modal="true"`
- brak `aria-labelledby` wskazującego na `.modal-title`
- brak focus trapu — Tab wychodzi z modala
- brak Esc → close
- brak return-focus na element który otworzył modal
- `resize: both` w CSS (linia 902) — niespotykane dla modala aplikacyjnego, robi UX chaos

**Skutek:** klawiaturowicze i screen readery praktycznie nie obsłużą Close-Month-Pipeline (jednej z dwóch krytycznych funkcji apki).

**Fix:** napisz `<Modal>` komponent (50 linii) z:
- `<dialog>` HTML (focus trap free) albo focus-trap-react
- Esc handler
- aria-* attrs
- return-focus

Albo zainstaluj `@radix-ui/react-dialog` (zero CSS bagażu, sam mechanizm) — to jest ten moment gdzie warto.

### P0-4. User dropdown na `<div onClick>`

**`src/components/Header.jsx:52`**: `<div className="user" onClick={...}>` zamiast `<button>`:
- brak Enter/Space activation
- brak focus visible
- brak `aria-expanded`, `aria-haspopup`
- nie da się zamknąć przez Esc
- click outside też nie zamyka (nie ma listenera)

**Fix:** zamień na `<button aria-haspopup="menu" aria-expanded={open}>` + click-outside listener + Esc handler. ~30 linii.

### P0-5. Sortable tabele bez `aria-sort` i klawiatury

**`src/views/FilesView.jsx:419-435`** — `<th className="sortable" onClick={() => toggleSort('name')}>`:
- klikalne, ale `<th>` nie jest w naturalnym tab orderze
- brak `aria-sort="ascending|descending|none"`
- brak `tabindex="0"` + Enter handler

**Fix:** użyj `<button>` wewnątrz `<th>`, dodaj `aria-sort`. SortInd pokazuje ↑↓ ale nie jest dostępne dla AT.

### P0-6. `window.confirm()` dla destrukcyjnych akcji × 3

`FilesView.jsx:109`, `CloseView.jsx:64`, `CloseView.jsx:578` — natywny dialog:
- nie zlokalizowany (zawsze PL niezależnie od `lang`)
- nie da się stylować pod brand
- nie informuje o konsekwencjach (np. "plik zostanie usunięty z Azure Blob — operacja nieodwracalna")
- blokuje wątek, w MS Edge dialogi natywne wyglądają obco

**Fix:** użyj komponentu `<ConfirmDialog>` (po naprawieniu Modala w P0-3). Treść lokalizowana, opisuje konsekwencje, ma "Cancel" jako default focus.

---

## P1 — WAŻNE (UX, hierarchia, brak afordancji)

### P1-1. Period chip wygląda klikalny, nie jest

**`src/components/Header.jsx:31`** + `src/styles/index.css:167-181`:
```jsx
<button className="period-chip btn-ghost" aria-label="Zmień okres">
  ...Okres: {PERIOD}
</button>
```
CSS daje cursor:pointer, hover na border-color, jest aria-label "Zmień okres" — ale `onClick` nie istnieje. Klikasz, nic się nie dzieje.

**Fix:** albo doimplementuj wybór miesiąca tu (i synchronizuj z `MONTHS_PL` + przekaż do widoków), albo zmień na `<span className="period-info">` bez cursor:pointer.

### P1-2. `8-stopniowy pipeline` — off-by-one bug w copy

`src/data.js:22-30` definiuje **7** kroków (n: 1..7).
`src/lang.js:38,87` mówi "Pipeline 8-stopniowy" / "8-step pipeline".
`src/views/CloseView.jsx:166` ma `STEP_EST_MS = [..., 3000]` — **8** wartości.

**Fix:** dodać 8. krok (jest miejsce na "verify in KSeF" / "save snapshot"?) lub poprawić copy na "7-stopniowy".

### P1-3. data-sidebar="cream" nie ma reguły CSS

**`src/App.jsx:47`**: `<div className="app" data-density="roomy" data-sidebar="cream">`.
`index.css:60-69` definiuje tylko `[data-sidebar="white"]` i `[data-sidebar="dark"]`. Wartość `cream` nie pasuje do żadnej reguły → spada na default `:root --sidebar-bg: #f0ece6`.

To **działa przypadkowo** (default też kremowy). Ale kiedy ktoś przyjdzie i zmieni domyślny `--sidebar-bg`, "cream" przestanie być cream-em.

**Fix:** dodać `[data-sidebar="cream"]` z explicit `--sidebar-bg: #f0ece6`, albo zmienić App.jsx na `data-sidebar="white"` lub usunąć atrybut.

### P1-4. Mobile: sidebar `display: none`, brak hamburgera

**`src/styles/index.css:1490-1499`**:
```css
@media (max-width: 900px) {
  .app { grid-template-areas: "header" "main"; }
  .sidebar { display: none; }
}
```

Sidebar znika na mobile. Header **nie ma menu hamburgerowego**, więc na telefonie nie ma jak nawigować. Apka jest unusable na mobile.

**Decyzja:** tool jest desktop-only? Jeśli tak — pokaż explicit komunikat "Otwórz na komputerze" zamiast okaleczonego UI. Jeśli nie — dodaj hamburger menu w Header.

### P1-5. Hardcoded strings poza i18n (PL-only)

Mimo że masz `lang.js` z PL/EN, w komponentach jest masa stringów PL-only:
- `FilesView.jsx`: "Brak plików", "Folder jest pusty lub nie pasuje do filtrów.", toast `Błąd ładowania: ...`
- `CloseView.jsx`: "Inbox — dokumenty do zamknięcia", "Sprawdź", "Zamykasz:", "Pomiń brakujące faktury kosztowe:", "Spróbuj ponownie", "Sukces", "Z ostrzeżeniami" itd.
- Confirm dialogi
- "Konfiguruj" tooltip w SettingsView
- "Twoje konto Entra ID", "Role Entra ID" — sekcje Settings nie tłumaczone

Przełączasz na EN — apka jest pl/en hybrydą.

**Fix:** uzupełnij `I18N` w `lang.js` o brakujące klucze, zamień stringi w JSX na `T.xxx`. ~80 nowych kluczy.

### P1-6. Mieszanka emoji vs Icon component

W tej samej apce jest:
- `Icon` z lucide-style paths (`<Icon name="check" />`) — w komponentach
- emoji jako tekst: `✅`, `❌`, `⚠️`, `🚫`, `↩`, `⚠`, `✖`, `·` (CloseView, InboxPanel checklist)

Skutek: w trybie ciemnym (jakby kiedyś pojawił), w innej rodzinie fontów emoji wyglądają obco. Nie skalują się ze stroke-width.

**Fix:** wszystko przez `<Icon>`. Dodaj do PATHS warianty `check-circle`, `x-circle`, `triangle-alert`. Emoji w toaście "⚠ {reason}" → `<Icon name="alert-circle" />`.

### P1-7. KPI value font-size 30px w fraunces — najmocniejszy element strony

`.kpi-value` jest 30px serif. To jest najbardziej kontrastowy element na stronie Files. Pytanie: czy KPI to faktycznie najważniejsza informacja, kiedy KPI revenue/sales są **w stuba** (`—` + "brak integracji API")?

Aktualnie tabela plików (właściwy kontent strony) jest wizualnie zdominowana przez stub-y KPI z `—`. **Pierwsze co przyciąga wzrok = puste.**

**Fix:** ukryj KPI z brakującą integracją (już masz feature flagi — wyłącz domyślnie). Albo zmniejsz `.kpi-value` do 22px kiedy wartość to "—". Albo daj wartość, której nie znasz: pokazuj `kpi_files_count` (jest live) jako pierwszy.

### P1-8. Hidden row-actions (`opacity: 0`)

**`src/styles/index.css:766-776`**:
```css
.row-actions { opacity: 0; transition: opacity .12s; }
table.files tbody tr:hover .row-actions { opacity: 1; }
```

Akcje download/delete są niewidoczne dopóki nie najedziesz. Discoverability problem — nowy użytkownik nie wie, że tabela ma akcje row-level.

**Fix:** `opacity: 0.4` w stanie spoczynku, `1` na hover. Albo zostaw 0, ale dodaj jeden global "Actions" trigger w nagłówku tabeli, który ustawia stan `show-actions` dla całej tabeli.

### P1-9. Avatar gradient: primary teal × accent brąz = mud

**`src/styles/index.css:195`**:
```css
background: linear-gradient(135deg, var(--primary), var(--accent));
```

Teal `#1a5f7a` + brąz `#7a4f1a` daje brudny szaro-zielono-brunatny. Inicjały białe na tym tle są ledwo czytelne pośrodku.

**Fix:** gradient teal → primary-600 (jeden kolor, dwa tony) albo solid primary. Lub zachowaj gradient ale tylko na avatarze 52px w UsersView, w 28px Header zostaw solid.

---

## P2 — POLISH (drobne, ale kumulują się)

### P2-1. Skala typografii — jest 9 rozmiarów, mogą być 4

W kodzie pojawiają się: 11, 11.5, 12, 12.5, 13, 13.5, 14, 15, 16, 18, 20, 22, 24, 28, 30, 34px.

**Fix:** zdefiniuj skalę i konsekwentnie używaj:
```css
--text-xs:   11px;  /* labels, eyebrows, badges */
--text-sm:   12px;  /* metadata, hints */
--text-base: 13px;  /* body / table cells */
--text-md:   14px;  /* nav, buttons */
--text-lg:   18px;  /* card titles */
--text-xl:   24px;  /* modal-title */
--text-2xl:  30px;  /* kpi-value */
--text-3xl:  34px;  /* page-title */
```

### P2-2. Kolory `text-3` na `bg` — kontrast borderline 4.13:1

`--text-3: #8a8278` na `--bg: #fafaf8` → ~4.13:1. WCAG AA dla **normal text** wymaga 4.5:1, dla **small text** 4.5:1. Klasa `.dim` używa text-3 dla 12-13px tekstów (daty, rozmiary) → fail AA.

**Fix:** ściemnij `--text-3` do `#7a7268` (~5.0:1). Albo użyj `--text-2 #55504a` dla tych miejsc.

### P2-3. Brak `:focus-visible` dla większości interaktywnych

Tylko `.search:focus-within` i `.field input:focus` mają outline. Reszta przycisków, nav-itemów, sortable th, filter-btn → brak focus indicatora (default browser outline jest przybity przez `outline: 0` na inputach albo przez przeźroczystość).

**Fix:** globalna reguła:
```css
:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
```

### P2-4. Toast: `position: fixed` ale `pointer-events: none` na wrap, sam toast bez aria-live

**`src/styles/index.css:1170-1194`** + `FilesView.jsx:549-555`:
- `aria-live="polite"` brak → screen reader nie zgłosi
- nie wszystkie widoki używają toast-wrap (SalesView na linii 138 używa same `<div className="toast">` bez wrappera ani aria)

**Fix:** centralny `<Toaster>` z `aria-live="polite" aria-atomic="true"`. Hooka `useToast()` zamiast lokalnych `setToast`.

### P2-5. Mocno podobne komponenty zduplikowane

- Avatar logic: `Header.jsx:16` i `UsersView.jsx:30-36` mają dwa różne kawałki kodu robiące to samo (initials, gradient bg, fontSize różny).
- File listing: `FilesView` i `InboxPanel` (CloseView) mają dwie różne implementacje listy plików z dropzonem.
- Search input: 4 widoki mają wklejony ten sam `<div className="search"><Icon><input>...`.
- Refresh button: 4 widoki mają identyczny `<button onClick={load}><Icon name="refresh" className={loading ? 'spinning' : ''} /></button>`.

**Fix:** wyciągnij `<Avatar>`, `<SearchInput>`, `<RefreshButton>`, `<FileTable>`. ~150 linii oszczędności.

### P2-6. Stub copy w produkcji

`CostView.jsx`: "Moduł w przygotowaniu — integracja z KSeF i skanowaniem OCR."
`KPIS_STUB` w data.js (87 420 zł, 43 faktury) — tylko mock, ale w `FilesView` masz live `kpi_files_count`. Mieszanka.
W `Settings`: "NIP: 000-000-00-00" placeholder.

**Fix:** zamiast "Moduł w przygotowaniu" → empty state z ikoną i CTA "Włącz powiadomienie kiedy będzie gotowe" (jeśli to możliwe) albo "Dlaczego jeszcze tego nie ma" (link do roadmapy).

### P2-7. Loading states niespójne

- Files: `<div className="spinner" />` w środku tabeli
- Sales/Products: tekstowe "Ładowanie…"
- CloseView: `addLog('Uruchamianie pipeline…', 'muted')` w log panelu
- Settings: brak (synchroniczny render z props auth)

**Fix:** ustal jeden styl. Skeleton rows dla tabel, spinner dla card-level, tekst dla inline operacji.

### P2-8. `font-feature-settings: "ss01", "cv11"` na body — globalnie

Linia 88. Aktywuje stylistic alternates Inter (ss01 = pojedyncze "g", cv11 = curved "l"). Część osób tego nie chce, część kocha. Detal — ale w tabelach z liczbami nie używaj alternatów. **Decyzja świadoma — zostaw albo wyłącz, ale zdecyduj.**

---

## Plan zmian — 5 etapów po ~½–1 dzień każdy

### Etap 1 — Naprawić fundament (P0-1, P0-3, P0-4) — **½ dnia**
1. Find/replace zepsutych tokenów: `--err`→`--error`, `--ok`→`--success`, dodaj `--success-50`, `--bg-2`→`--surface-2`
2. Zamień fallbacki `var(--token, X)` na `var(--token)` żeby przyszłe rozjazdy wybijały się od razu
3. Header user dropdown: `<div>` → `<button>` z aria-expanded + Esc + click-outside
4. Modal: dodaj `role="dialog"`, `aria-modal`, `aria-labelledby`, focus trap (rozważ Radix Dialog), Esc handler

**Po tym etapie:** apka jest spójna kolorystycznie i obsługiwalna z klawiatury.

### Etap 2 — Wyrównanie inline styles (P0-2) — **1 dzień**
1. Zinwentaryzuj 128 inline styli — pogrupuj po wzorcach
2. Wyodrębnij ~15 nowych klas CSS (`.sticky-toolbar`, `.account-grid`, `.modal-month-picker`, `.banner-success`, `.checklist-row`, `.history-table` etc.)
3. Migruj widoki — start od `CloseView` (57 inline → ~5)
4. Wyłącznie dynamiczne wartości zostają inline (`--kpi-accent: var(--primary)`)

**Po tym etapie:** zmiana koloru w `:root` propaguje się wszędzie. Reading flow w PRach też się normalizuje.

### Etap 3 — Komponentyzacja (P2-5) — **½ dnia**
1. `<Avatar>` — initials + gradient + size prop. UsersView i Header używają tego samego.
2. `<SearchInput>` — z X clear button. 4 widoki przestają duplikować.
3. `<RefreshButton>` — z prop `loading`.
4. `<DataTable>` — wrapper wokół `<table className="files">` z props sortable, density, empty-state.
5. `<ConfirmDialog>` — zamiast 3 × `window.confirm`.

**Po tym etapie:** ~200 linii kodu mniej, dodanie nowego widoku = składanie z klocków.

### Etap 4 — i18n + a11y polish (P1-5, P1-6, P0-5, P0-6, P2-3, P2-4) — **½ dnia**
1. Audyt stringów hardcoded → uzupełnij `I18N`. Zacznij od CloseView (najwięcej).
2. Wszystkie emoji statusowe → `<Icon>`.
3. `aria-sort` na sortable headers + Enter handler.
4. Globalny `:focus-visible`.
5. Toaster z `aria-live="polite"`.

**Po tym etapie:** EN jest pełnym EN, screen reader ogarnia główne flow.

### Etap 5 — Hierarchia & afordancje (P1-1, P1-2, P1-3, P1-4, P1-7, P1-8, P1-9, P2-2, P2-6) — **½ dnia**
1. Period chip: zaimplementuj wybór miesiąca **albo** zdejmij cursor:pointer + przerób na `<span>`.
2. Pipeline copy: 7 vs 8 — zdecyduj i napraw.
3. `data-sidebar="cream"` → dodaj regułę CSS lub zmień atrybut.
4. Mobile: hamburger lub komunikat "desktop only".
5. `kpi-revenue/sales` z brakiem API: `kpi-meta` → "Włącz integrację" jako CTA, nie "brak integracji API".
6. `.row-actions` opacity 0 → 0.4.
7. Avatar gradient: zamień na primary→primary-600.
8. Kontrast text-3: `#8a8278` → `#7a7268`.
9. CostView empty state: lepszy copy.

**Po tym etapie:** apka przestaje wysyłać sprzeczne sygnały (klikalne ≠ klikalne, mobile ≠ mobile, stub ≠ realna feature).

### Etap 6 (opcjonalny) — Migracja na shadcn/ui + Tailwind — **2-3 dni**
Jeśli chcesz iść dalej i jednocześnie odzyskać kontrolę nad ciemnym trybem, accessibility (Radix), formami, datepickerami:

1. Dodaj Tailwind + shadcn-init
2. Zmapuj swoje tokeny CSS na zmienne shadcn (themes-cli pomaga)
3. Zachowaj swój charakter wizualny (Inter+Fraunces, paleta, density modes) jako custom theme
4. Komponenty 1:1 z shadcn: Button, Input, Select, Dialog (= twój Modal), DropdownMenu (= user dropdown), Table (z @tanstack/react-table do sortowania/filterów), Toast (sonner)
5. Usuń `index.css` (1500 linii) → ~200 linii custom + reszta z Tailwind/shadcn

**Trade-off:** dużo pracy, ale potem każda nowa funkcja jest 5× szybsza. **Polecam dopiero po Etap 1-3.**

---

## Aneks A — 30-minutowe quick-winy (zrób przed Etap 1)

Pojedyncze zmiany które dadzą natychmiastowy efekt:

1. **Globalny `:focus-visible`** w `index.css` (5 min)
2. **Kontrast text-3** zmień na `#7a7268` w `:root` (1 min)
3. **`.row-actions { opacity: 0.4 }`** zamiast `0` (1 min)
4. **Period chip** — usuń `cursor: pointer` z `.period-chip` aż nie zaimplementujesz click-handlera (1 min — eliminuje fake afordancję)
5. **Pipeline 8→7** w `lang.js:38,87` (1 min)
6. **`data-sidebar="cream"`** w App.jsx → `data-sidebar="white"` lub usuń atrybut (1 min)
7. **Toaster aria-live** dodaj `role="status" aria-live="polite"` do `.toast-wrap` (2 min)

To 7 zmian × ~15 min testów = ½ godziny i apka skacze o pół ligi w górę.

---

## Aneks B — czego brakuje (świadome braki, do decyzji)

Te rzeczy nie są bug-ami, ale typowy panel B2B na tym etapie zwykle je ma. Decyzja produktowa, nie designerska:

- **Dark mode** — masz `data-sidebar="dark"` częściowy, ale brak globalnego `data-theme="dark"`. Łatwe do dodania bo masz tokeny.
- **Empty/Error states ilustracje** — wszystkie empty są "tekst + ikona". Linear/Notion mają ilustracyjne pustki które robią ciepło.
- **Skeleton loading** zamiast spinnerów dla tabel.
- **Keyboard shortcuts** — w panelu accountant `Cmd+K` do command palette to QoL, nie luksus.
- **Filtry zaawansowane** w SalesView (zakres dat, status multi-select, kontrahent dropdown) — teraz tylko search + miesiąc.
- **Eksport CSV/XLSX** z tabel — częste w panelach księgowych.
- **Notyfikacje** w bell-ikonie — bell jest, dropdown nie istnieje.

---

**Następny krok:** powiedz na którym etapie zacząć (Aneks A = 30 min quick-winy są najtańszą drogą), albo czy zrobić Etap 1 od razu — po akceptacji wchodzę w pliki i robię diffy.

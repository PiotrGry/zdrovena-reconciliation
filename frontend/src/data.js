export const MONTHS_PL = ['Styczeń', 'Luty', 'Marzec', 'Kwiecień', 'Maj', 'Czerwiec', 'Lipiec', 'Sierpień', 'Wrzesień', 'Październik', 'Listopad', 'Grudzień']
export const MONTH_SHORT = ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru']

export const fmtBytes = b => {
    if (b == null) return '—'
    if (b < 1024) return `${b} B`
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`
    return `${(b / (1024 * 1024)).toFixed(2)} MB`
}

export const fmtDate = iso => {
    if (!iso) return '—'
    const d = new Date(iso)
    return d.toLocaleDateString('pl-PL', { day: '2-digit', month: 'short', year: 'numeric' })
}

export const fmtPLN = n => {
    if (n == null) return '—'
    return new Intl.NumberFormat('pl-PL', { style: 'currency', currency: 'PLN' }).format(n)
}

export const PIPELINE_STEPS = [
    { n: 1, key: 'Pre-flight',         title: 'Preflight checks',    est: '~2 s' },
    { n: 2, key: 'Folder structure',   title: 'Struktura folderów',  est: '~1 s' },
    { n: 3, key: 'Sales invoices',     title: 'Faktury sprzedaży',   est: '~5 s' },
    { n: 4, key: 'JPK & VAT reports',  title: 'Raporty JPK & VAT',   est: '~8 s' },
    { n: 5, key: 'Cost invoices',      title: 'Faktury kosztowe',    est: '~12 s' },
    { n: 6, key: 'Bank statement check', title: 'Wyciąg bankowy',    est: '~1 s' },
    { n: 7, key: 'ZIP archive',        title: 'Archiwum ZIP',        est: '~4 s' },
    { n: 8, key: 'Email',              title: 'Wyślij e-mail',       est: '~3 s' },
]

// Dry-run step keys contain "(dry-run)" suffix — normalize for matching
export const normalizeStepKey = (key) => key.replace(/\s*\(dry-run\)/i, '')

export const KPIS_STUB = [
    { label: 'Przychód (netto)', value: '87 420 zł', meta: 'kwiecień 2026', accent: 'var(--primary)' },
    { label: 'Faktur sprzedaży', value: '43', meta: '2 szkice', accent: 'var(--accent)' },
    { label: 'Pliki w kontenerze', value: '1 284', meta: '3 wgrane dziś', accent: 'var(--success)' },
    { label: 'Status pipeline', value: 'Gotowe', meta: 'Ostatni: 31 mar ✓', accent: 'var(--warning)' },
]

export const SALES_INVOICES = [
    { id: 'FV/2026/04/001', date: '2026-04-03', buyer: 'Hurtownia Zdrowie Sp. z o.o.', net: 14200, vat: 3266, gross: 17466, status: 'paid' },
    { id: 'FV/2026/04/002', date: '2026-04-05', buyer: 'BioFresh Polska S.A.', net: 8750, vat: 2012.5, gross: 10762.5, status: 'paid' },
    { id: 'FV/2026/04/003', date: '2026-04-07', buyer: 'Eko Zdrówko Sklep', net: 3200, vat: 736, gross: 3936, status: 'unpaid' },
    { id: 'FV/2026/04/004', date: '2026-04-10', buyer: 'Natura Wigor S.C.', net: 6420, vat: 1476.6, gross: 7896.6, status: 'paid' },
    { id: 'FV/2026/04/005', date: '2026-04-12', buyer: 'MedVita Distribution', net: 11100, vat: 2553, gross: 13653, status: 'overdue' },
    { id: 'FV/2026/04/006', date: '2026-04-15', buyer: 'Kowalski Jan — detalista', net: 420, vat: 96.6, gross: 516.6, status: 'paid' },
    { id: 'FV/2026/04/007', date: '2026-04-18', buyer: 'Green Market Sp. z o.o.', net: 9340, vat: 2148.2, gross: 11488.2, status: 'issued' },
    { id: 'FV/2026/04/008', date: '2026-04-20', buyer: 'Hurtownia Zdrowie Sp. z o.o.', net: 16800, vat: 3864, gross: 20664, status: 'paid' },
    { id: 'FV/2026/04/009', date: '2026-04-22', buyer: 'Apteka Słoneczna', net: 2100, vat: 483, gross: 2583, status: 'unpaid' },
    { id: 'FV/2026/04/010', date: '2026-04-25', buyer: 'NutriPlus Dystrybucja', net: 7890, vat: 1814.7, gross: 9704.7, status: 'issued' },
    { id: 'FV/2026/04/011', date: '2026-04-28', buyer: 'HealthLine Polska', net: 7200, vat: 1656, gross: 8856, status: 'draft' },
]

export const PRODUCTS = [
    { sku: 'ZD-001', name: 'Woda Zdrovena Naturalna', capacity: '500 ml', pkg: '12-pack', price: 28.80, active: true },
    { sku: 'ZD-002', name: 'Woda Zdrovena Naturalna', capacity: '1.5 L', pkg: '6-pack', price: 34.20, active: true },
    { sku: 'ZD-003', name: 'Woda Zdrovena Gazowana', capacity: '500 ml', pkg: '12-pack', price: 30.00, active: true },
    { sku: 'ZD-004', name: 'Woda Zdrovena Gazowana', capacity: '1.5 L', pkg: '6-pack', price: 36.60, active: true },
    { sku: 'ZD-005', name: 'Woda Zdrovena Premium', capacity: '750 ml', pkg: '6-pack', price: 52.20, active: true },
    { sku: 'ZD-006', name: 'Woda Zdrovena Lekko Gaz.', capacity: '1 L', pkg: '6-pack', price: 38.40, active: true },
    { sku: 'ZD-007', name: 'Woda Zdrovena dla Dzieci', capacity: '330 ml', pkg: '24-pack', price: 44.40, active: true },
    { sku: 'ZD-008', name: 'Woda Zdrovena Mineralna+', capacity: '1.5 L', pkg: '6-pack', price: 41.40, active: false },
    { sku: 'ZD-009', name: 'Woda Zdrovena Horeca 5 L', capacity: '5 L', pkg: 'karton 4', price: 96.00, active: true },
]

export const ORDERS = [
    { id: 'ZAM/2026/04/0142', date: '2026-04-02', customer: 'Hurtownia Zdrowie Sp. z o.o.', items: 6, wz: 'WZ/0142', status: 'delivered' },
    { id: 'ZAM/2026/04/0143', date: '2026-04-04', customer: 'BioFresh Polska S.A.', items: 4, wz: 'WZ/0143', status: 'delivered' },
    { id: 'ZAM/2026/04/0144', date: '2026-04-08', customer: 'Eko Zdrówko Sklep', items: 3, wz: 'WZ/0144', status: 'delivered' },
    { id: 'ZAM/2026/04/0145', date: '2026-04-11', customer: 'Natura Wigor S.C.', items: 5, wz: 'WZ/0145', status: 'sent' },
    { id: 'ZAM/2026/04/0146', date: '2026-04-14', customer: 'MedVita Distribution', items: 8, wz: '—', status: 'prep' },
    { id: 'ZAM/2026/04/0147', date: '2026-04-17', customer: 'Green Market Sp. z o.o.', items: 4, wz: '—', status: 'prep' },
    { id: 'ZAM/2026/04/0148', date: '2026-04-20', customer: 'Hurtownia Zdrowie Sp. z o.o.', items: 9, wz: '—', status: 'new' },
    { id: 'ZAM/2026/04/0149', date: '2026-04-22', customer: 'Apteka Słoneczna', items: 2, wz: '—', status: 'new' },
    { id: 'ZAM/2026/04/0150', date: '2026-04-25', customer: 'NutriPlus Dystrybucja', items: 6, wz: '—', status: 'new' },
    { id: 'ZAM/2026/04/0151', date: '2026-04-27', customer: 'HealthLine Polska', items: 3, wz: '—', status: 'new' },
]

// Feature flags — ustaw na true żeby włączyć widok/funkcję
export const FEATURES = {
    orders: false,
    products: false,
    users: false,
    shipping: true,
    dlq: true,
    damage: true,

    // KPI cards in FilesView
    // kpi_files_count  — liczba plików wyliczana live z /api/files (zawsze dostępne)
    // kpi_pipeline     — status pipeline z /api/close/state (zawsze dostępne)
    // kpi_revenue      — przychód netto (wymaga integracji z Fakturownia — brak endpointu)
    // kpi_sales_count  — liczba faktur sprzedaży (wymaga integracji z Fakturownia — brak endpointu)
    kpi_files_count: true,
    kpi_pipeline: true,
    kpi_revenue: false,
    kpi_sales_count: false,
}

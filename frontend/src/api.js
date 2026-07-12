// Wspólny helper do wywołań API.
//
// Rozwiązuje trzy źródła "enigmatycznych" komunikatów:
//  1. NIGDY nie używa res.statusText (pusty w HTTP/2 za proxy Azure SWA).
//  2. Parsuje kopertę błędu backendu ({error_code, message_pl, details,
//     correlation_id}) — z bezpiecznym fallbackiem, gdy koperty jeszcze nie ma.
//  3. Zawsze rzuca Error z czytelną, polską treścią zamiast połykać błąd.
export async function fetchJson(url, { token, ...opts } = {}) {
    const res = await fetch(url, {
        ...opts,
        headers: {
            ...(opts.headers || {}),
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
    })
    if (!res.ok) {
        const body = await res.json().catch(() => null)
        const msg =
            body?.message_pl ||
            body?.detail ||
            `Błąd serwera (HTTP ${res.status})` // NIGDY pusty statusText
        const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
        err.code = body?.error_code
        err.correlationId = body?.correlation_id
        err.details = body?.details
        err.status = res.status
        throw err
    }
    return res.status === 204 ? null : res.json()
}

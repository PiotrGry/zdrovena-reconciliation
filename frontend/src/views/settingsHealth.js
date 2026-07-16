export function statusKind(status) {
    if (status === 'healthy') return 'ok'
    if (status === 'unavailable') return 'err'
    return 'warn'
}

export function statusLabel(status) {
    if (status === 'healthy') return 'Zdrowa'
    if (status === 'degraded') return 'Ostrzeżenie'
    if (status === 'unavailable') return 'Niedostępna'
    if (status === 'not_configured') return 'Brak konfiguracji'
    return 'Uwaga'
}

export function boolLabel(value) {
    return value ? 'tak' : 'nie'
}

export function formatCheckedAt(value) {
    if (!value) return '—'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    return date.toLocaleString('pl-PL', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    })
}

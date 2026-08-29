// Reading the shape of a sync result.


export function syncStat(result, key) {
    if (!result) return 0
    return ['allegro', 'shopify'].reduce((sum, source) => {
        const value = result[source]?.[key]
        return sum + (Number.isFinite(value) ? value : 0)
    }, 0)
}

export function syncErrorCount(result) {
    if (!result) return 0
    return ['allegro', 'shopify'].reduce((sum, source) => {
        const sourceResult = result[source]
        if (!sourceResult) return sum
        return sum + (sourceResult.error ? 1 : 0) + (Number(sourceResult.errors) || 0)
    }, 0)
}

export function syncSummary(result) {
    const created = syncStat(result, 'created')
    const updated = syncStat(result, 'updated')
    const unchanged = syncStat(result, 'unchanged') + syncStat(result, 'skipped') + syncStat(result, 'skipped_duplicate')
    const errors = syncErrorCount(result)
    return `Synchronizacja zakończona: ${created} nowe, ${updated} zaktualizowanych, ${unchanged} bez zmian, ${errors} błędów.`
}

export function apiErrorMessage(body, response) {
    const message = body?.message_pl || body?.detail || `${response.status}`
    const correlationId = body?.correlation_id || response.headers?.get?.('X-Correlation-ID')
    return correlationId ? `${message} (ID: ${correlationId})` : message
}

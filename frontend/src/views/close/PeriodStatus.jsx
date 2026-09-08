const SEVERITY_COLOR = {
    blocker: 'var(--error)',
    error: 'var(--error)',
    warning: 'var(--warn, #b45309)',
}

const STATUS_MARK = { ok: '✓', missing: '✗', invalid: '!' }

function formatMoment(value) {
    if (!value) return '—'
    const parsed = new Date(value)
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('pl-PL')
}

/**
 * How the period stands right now — a question, not a move.
 *
 * Deliberately has no controls. Closing a month is iterative: invoices arrive
 * over weeks, so "is it complete yet?" gets asked far more often than any step
 * gets run. Until this panel existed the only way to ask was to execute the
 * `check` stage, which claims the period, needs an accountant role and writes
 * to the run. A button here would put the operator straight back into that.
 */
export function PeriodStatus({ inspection }) {
    if (!inspection) return null

    const { documents = [], issues = [], metrics = {}, computed_at: computedAt, run } = inspection
    const missing = documents.filter(doc => doc.status !== 'ok')

    return (
        <div className="card">
            <div className="card-head">
                <span className="card-title">Stan okresu</span>
                <span className="card-sub" data-testid="period-status-computed-at">
                    sprawdzono {formatMoment(computedAt)}
                </span>
            </div>
            <div style={{ padding: '16px 20px' }}>
                <div style={{ marginBottom: 12 }}>
                    {metrics.ready && issues.length === 0
                        ? <strong>Komplet — okres gotowy do zamknięcia</strong>
                        : <strong>Brakuje {missing.length} z {documents.length} dokumentów</strong>}
                </div>

                <div className="dim" style={{ fontSize: '0.88em', marginBottom: 12 }}>
                    {run ? `Przebieg: ${run.status}` : 'Zamykanie nierozpoczęte'}
                </div>

                {missing.length > 0 && (
                    <div style={{ marginBottom: 12 }}>
                        {missing.map(doc => (
                            <div key={doc.id} style={{ display: 'flex', gap: 8 }}>
                                <span className="mono">{STATUS_MARK[doc.status] ?? '?'}</span>
                                <span>{doc.label}</span>
                            </div>
                        ))}
                    </div>
                )}

                {issues.map(issue => (
                    <div
                        key={issue.id}
                        style={{ fontSize: '0.88em', color: SEVERITY_COLOR[issue.severity] }}>
                        {issue.message}
                    </div>
                ))}
            </div>
        </div>
    )
}

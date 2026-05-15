import { Icon } from '../../components/Icon'

/**
 * Karty z metrykami po zakończeniu zamknięcia: faktury sprzedaży, brutto,
 * faktury kosztowe, status emaila. Plus opcjonalne sekcje warnings/errors.
 */
export function ResultPanel({ result }) {
    if (!result) return null

    const grossPLN = result.sales_gross_total
        ? `${Number(result.sales_gross_total).toLocaleString('pl-PL')} PLN`
        : '—'

    const metrics = [
        {
            key: 'sales',
            label: 'Faktury sprzedaży',
            value: result.sales_invoice_count ?? '—',
            icon: 'invoice',
        },
        {
            key: 'gross',
            label: 'Brutto',
            value: grossPLN,
            icon: 'zap',
        },
        {
            key: 'costs',
            label: 'Faktury kosztowe',
            value: result.cost_invoice_count ?? '—',
            icon: 'archive',
        },
        {
            key: 'email',
            label: 'E-mail do księgowej',
            value: result.email_sent ? 'Wysłany' : 'Nie wysłany',
            icon: 'check',
            kind: result.email_sent ? 'ok' : 'warn',
        },
    ]

    return (
        <section className="card result-panel" aria-labelledby="result-panel-title">
            <div className="card-head">
                <span className="card-title" id="result-panel-title">
                    <Icon name="check" size={14} /> Wynik zamknięcia
                </span>
            </div>
            <div className="result-metrics">
                {metrics.map(m => (
                    <div
                        key={m.key}
                        className={`result-metric${m.kind ? ` is-${m.kind}` : ''}`}
                    >
                        <div className="result-metric-label">{m.label}</div>
                        <div className="result-metric-value">{m.value}</div>
                    </div>
                ))}
            </div>
            {result.warnings?.length > 0 && (
                <div className="result-issues">
                    <div className="result-issues-title result-issues-warn">
                        <Icon name="alert-circle" size={13} /> Ostrzeżenia ({result.warnings.length})
                    </div>
                    <ul>
                        {result.warnings.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                </div>
            )}
            {result.errors?.length > 0 && (
                <div className="result-issues">
                    <div className="result-issues-title result-issues-err">
                        <Icon name="x" size={13} /> Błędy ({result.errors.length})
                    </div>
                    <ul>
                        {result.errors.map((e, i) => <li key={i}>{e}</li>)}
                    </ul>
                </div>
            )}
        </section>
    )
}

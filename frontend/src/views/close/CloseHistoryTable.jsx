import { useState, useCallback, useEffect } from 'react'
import { useAuth } from '../../auth'
import { useToast } from '../../components/Toast'
import { Icon } from '../../components/Icon'
import { PIPELINE_STEPS } from '../../data'
import { fetchJson } from '../../api'
import { usePolling } from '../../hooks/usePolling'

const STATUS_CONFIG = {
    success: { kind: 'ok',   label: 'Sukces',          desc: 'Wszystkie kroki wykonane, e-mail wysłany' },
    partial: { kind: 'warn', label: 'Z ostrzeżeniami', desc: 'ZIP OK, ale e-mail zablokowany (brak faktur kosztowych lub JPK)' },
    blocked: { kind: 'warn', label: 'Brak plików',     desc: 'Zablokowany przez preflight' },
    error:   { kind: 'err',  label: 'Błąd wykonania',  desc: 'Pipeline crashnął w trakcie' },
}

/**
 * Tabela ostatnich zamknięć. Akcje: wznów / uruchom ponownie, usuń.
 * Renderuje się tylko jeśli są wpisy w historii.
 */
export function CloseHistoryTable({ refreshKey = 0 }) {
    const { getToken } = useAuth()
    const { pushToast } = useToast()
    const [history, setHistory] = useState([])
    const [loading, setLoading] = useState(true)

    // silent=true dla odświeżania w tle (polling): bez toasta błędu, zostawia
    // ostatnie dobre dane.
    const load = useCallback(async ({ silent = false } = {}) => {
        try {
            const token = await getToken()
            setHistory(await fetchJson('/api/close/history?limit=15', { token }))
        } catch (e) {
            if (!silent) {
                pushToast({ kind: 'error', msg: `Nie udało się wczytać historii zamknięć: ${e.message}` })
            }
        } finally {
            if (!silent) setLoading(false)
        }
    }, [getToken, pushToast])

    useEffect(() => {
        const timer = window.setTimeout(() => { void load() }, 0)
        return () => window.clearTimeout(timer)
    }, [load, refreshKey])
    usePolling(() => load({ silent: true }), 20_000)

    const deleteEntry = async (ts) => {
        if (!window.confirm('Usuń ten wpis z historii?')) return
        try {
            const token = await getToken()
            await fetchJson(`/api/close/history/${encodeURIComponent(ts)}`, {
                method: 'DELETE',
                token,
            })
            setHistory(prev => prev.filter(h => h.ts !== ts))
        } catch (e) {
            pushToast({ kind: 'error', msg: `Nie udało się usunąć wpisu: ${e.message}` })
        }
    }

    if (loading || !history.length) return null

    return (
        <section className="card close-history" aria-labelledby="close-history-title">
            <div className="card-head">
                <span className="card-title" id="close-history-title">
                    <Icon name="clock" size={14} /> Historia zamknięć
                </span>
                <span className="card-sub">{history.length} ostatnich</span>
            </div>
            <table className="files">
                <thead>
                    <tr>
                        <th>Miesiąc</th>
                        <th>Status</th>
                        <th style={{ textAlign: 'right' }}>Faktury</th>
                        <th style={{ textAlign: 'right' }}>Brutto</th>
                        <th>Kroki</th>
                        <th>Data</th>
                        <th style={{ width: 80 }} aria-label="Akcje" />
                    </tr>
                </thead>
                <tbody>
                    {history.map((h) => {
                        const cfg = STATUS_CONFIG[h.status] ?? { kind: 'default', label: h.status, desc: '' }
                        const steps = h.steps_completed ?? 0
                        const incomplete = steps < PIPELINE_STEPS.length
                        const grossPLN = h.sales_gross_total
                            ? `${Number(h.sales_gross_total).toLocaleString('pl-PL')} PLN`
                            : '—'
                        const tip = cfg.desc + (h.error ? `\n\nBłąd: ${h.error}` : '')
                        return (
                            <tr key={h.ts}>
                                <td>
                                    <strong>{h.month_name} {h.year}</strong>
                                    {h.dry_run && <span className="badge-dry">DRY</span>}
                                </td>
                                <td title={tip}>
                                    <span className={`pill ${cfg.kind}`}>
                                        <span className="dot" />
                                        {cfg.label}
                                    </span>
                                </td>
                                <td className="mono" style={{ textAlign: 'right' }}>
                                    {(h.status === 'success' || h.status === 'partial')
                                        ? (h.sales_invoice_count ?? '—')
                                        : '—'}
                                </td>
                                <td className="mono" style={{ textAlign: 'right' }}>{grossPLN}</td>
                                <td className={`mono ${incomplete ? 'warn' : 'dim'}`}>
                                    {steps}/{PIPELINE_STEPS.length}
                                </td>
                                <td className="mono dim">
                                    {h.ts ? new Date(h.ts).toLocaleString('pl-PL') : '—'}
                                </td>
                                <td>
                                    <div className="row-actions">
                                        <button
                                            type="button"
                                            className="icon-btn danger"
                                            title="Usuń z historii"
                                            aria-label={`Usuń wpis ${h.month_name} ${h.year}`}
                                            onClick={() => deleteEntry(h.ts)}
                                        >
                                            <Icon name="trash" size={14} />
                                        </button>
                                    </div>
                                </td>
                            </tr>
                        )
                    })}
                </tbody>
            </table>
        </section>
    )
}

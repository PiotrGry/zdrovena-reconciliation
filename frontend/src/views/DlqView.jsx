import { Fragment, useState, useCallback, useEffect } from 'react'
import { useAuth } from '../auth'
import { useToast } from '../components/Toast'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'
import { discardDlqEntry, getDlqEntries, retryDlqEntry } from '../api/endpoints'
import { usePolling } from '../hooks/usePolling'

// Widok DLQ (dead-letter queue) — nieudane próby utworzenia draftu przesyłki.
// Endpointy istniały, ale nie były dostępne z UI: operator musiał używać curl.
// Tu: lista, podgląd payloadu, ponów (retry) i usuń (discard) z feedbackiem.

function orderLabel(payload) {
    if (!payload || typeof payload !== 'object') return '—'
    return (
        payload.order_number ??
        payload.name ??
        payload.checkoutForm?.id ??
        payload.id ??
        '—'
    )
}

function fmtTs(ts) {
    if (!ts) return '—'
    const d = new Date(ts)
    return Number.isNaN(d.getTime()) ? ts : d.toLocaleString('pl-PL')
}

export default function DlqView() {
    const { getToken } = useAuth()
    const { pushToast } = useToast()
    const [entries, setEntries] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)
    const [busy, setBusy] = useState(new Set())
    const [expanded, setExpanded] = useState(new Set())

    // silent=true dla odświeżania w tle (polling): bez spinnera i bez podmiany
    // listy na komunikat błędu — zostawia ostatnie dobre dane.
    const load = useCallback(async ({ silent = false } = {}) => {
        if (!silent) {
            setLoading(true)
            setError(null)
        }
        try {
            const token = await getToken()
            const data = await getDlqEntries({ token })
            setEntries(data.entries ?? [])
            if (silent) setError(null)
        } catch (e) {
            if (!silent) setError(e.message)
        } finally {
            if (!silent) setLoading(false)
        }
    }, [getToken])

    useEffect(() => { load() }, [load])
    usePolling(() => load({ silent: true }), 20_000)

    const withBusy = (id, fn) => async () => {
        setBusy(s => new Set([...s, id]))
        try {
            await fn()
        } finally {
            setBusy(s => { const n = new Set(s); n.delete(id); return n })
        }
    }

    const retry = (id) => withBusy(id, async () => {
        try {
            const token = await getToken()
            await retryDlqEntry({ id, token })
            pushToast({ kind: 'success', msg: 'Wpis DLQ ponowiony — draft utworzony.' })
            await load({ silent: true })
        } catch (e) {
            pushToast({ kind: 'error', msg: `Ponowienie nie powiodło się: ${e.message}` })
        }
    })

    const discard = (id) => withBusy(id, async () => {
        if (!window.confirm('Usunąć wpis DLQ trwale? Zamówienie zostanie zignorowane i nie będzie już próby utworzenia draftu.')) return
        try {
            const token = await getToken()
            await discardDlqEntry({ id, token })
            pushToast({ kind: 'success', msg: 'Wpis DLQ usunięty.' })
            await load({ silent: true })
        } catch (e) {
            pushToast({ kind: 'error', msg: `Usunięcie nie powiodło się: ${e.message}` })
        }
    })

    const toggleExpand = (id) => setExpanded(s => {
        const n = new Set(s)
        n.has(id) ? n.delete(id) : n.add(id)
        return n
    })

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead
                title="Kolejka błędów (DLQ)"
                sub="Nieudane próby utworzenia draftu przesyłki. Ponów po usunięciu przyczyny albo odrzuć wpis."
                actions={
                    <button className="btn btn-ghost btn-sm" onClick={() => load()} disabled={loading} title="Odśwież">
                        <Icon name="refresh" size={14} className={loading ? 'spinning' : ''} />
                    </button>
                }
            />

            {error && (
                <div className="error-banner">Nie udało się wczytać kolejki DLQ: {error}</div>
            )}

            <div className="card">
                <div style={{ overflowX: 'auto' }}>
                    <table className="files">
                        <thead>
                            <tr>
                                <th>Utworzono</th>
                                <th>Źródło</th>
                                <th>Zamówienie</th>
                                <th style={{ textAlign: 'right' }}>Próby</th>
                                <th>Ostatni błąd</th>
                                <th style={{ textAlign: 'right' }}>Akcje</th>
                            </tr>
                        </thead>
                        <tbody>
                            {loading && (
                                <tr><td colSpan={6} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-3)' }}>Ładowanie…</td></tr>
                            )}
                            {!loading && !error && entries.length === 0 && (
                                <tr><td colSpan={6} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-3)' }}>Kolejka DLQ jest pusta — brak nieudanych draftów.</td></tr>
                            )}
                            {!loading && entries.map(entry => {
                                const isBusy = busy.has(entry.id)
                                const isOpen = expanded.has(entry.id)
                                return (
                                    <Fragment key={entry.id}>
                                        <tr data-testid={`dlq-row-${entry.id}`}>
                                            <td className="mono">{fmtTs(entry.created_at)}</td>
                                            <td><Pill kind="default">{entry.source ?? 'shopify'}</Pill></td>
                                            <td className="mono" style={{ fontWeight: 500 }}>{orderLabel(entry.payload)}</td>
                                            <td className="mono" style={{ textAlign: 'right' }}>{entry.retries ?? 0}</td>
                                            <td style={{ maxWidth: 320, color: 'var(--error)' }}>{entry.last_error ?? '—'}</td>
                                            <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                                                <button className="btn btn-ghost btn-sm" onClick={() => toggleExpand(entry.id)} title="Podgląd payloadu">
                                                    <Icon name={isOpen ? 'chevronUp' : 'chevronDown'} size={13} /> Payload
                                                </button>
                                                <button
                                                    className="btn btn-ghost btn-sm"
                                                    data-testid={`dlq-retry-${entry.id}`}
                                                    onClick={retry(entry.id)}
                                                    disabled={isBusy}
                                                    title="Ponów utworzenie draftu"
                                                >
                                                    {isBusy
                                                        ? <><Icon name="loader" size={13} className="spin" /> Ponawianie…</>
                                                        : <><Icon name="refresh" size={13} /> Ponów</>}
                                                </button>
                                                <button className="btn btn-ghost btn-sm" onClick={discard(entry.id)} disabled={isBusy} title="Usuń wpis bez ponawiania">
                                                    <Icon name="trash" size={13} /> Usuń
                                                </button>
                                            </td>
                                        </tr>
                                        {isOpen && (
                                            <tr>
                                                <td colSpan={6} style={{ background: 'var(--bg-2, #f6f6f6)' }}>
                                                    <pre className="mono" style={{ margin: 0, padding: '8px 12px', fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 320, overflow: 'auto' }}>
                                                        {JSON.stringify(entry.payload, null, 2)}
                                                    </pre>
                                                </td>
                                            </tr>
                                        )}
                                    </Fragment>
                                )
                            })}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    )
}

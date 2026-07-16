import { useState, useEffect, useCallback } from 'react'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'
import { fmtDate, fmtPLN, MONTHS_PL } from '../data'
import { usePolling } from '../hooks/usePolling'
import { getSalesInvoices } from '../api/endpoints'

const STATUS_KIND = {
    paid: 'ok',
    unpaid: 'warn',
    overdue: 'err',
    issued: 'info',
    draft: 'default',
}

export default function SalesView() {
    const { getToken } = useAuth()
    const { t, lang } = useT()
    const T = t[lang]
    const [search, setSearch] = useState('')
    const [items, setItems] = useState([])
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState(null)
    const [toast, setToast] = useState(null)

    const now = new Date()
    const [year, setYear] = useState(now.getFullYear())
    const [month, setMonth] = useState(now.getMonth() + 1)
    const years = [now.getFullYear() - 1, now.getFullYear()]

    const showToast = msg => { setToast(msg); setTimeout(() => setToast(null), 3000) }

    // silent=true dla odświeżania w tle (polling): bez spinnera, bez toasta błędu,
    // zostawia ostatnie dobre dane.
    const loadInvoices = useCallback(async ({ silent = false } = {}) => {
        if (!silent) {
            setLoading(true)
            setError(null)
        }
        try {
            const token = await getToken()
            setItems(await getSalesInvoices({ year, month, token }))
        } catch (e) {
            if (!silent) {
                setError(e.message)
                showToast(`Błąd ładowania: ${e.message}`)
            }
        } finally {
            if (!silent) setLoading(false)
        }
    }, [getToken, year, month])

    useEffect(() => { loadInvoices() }, [loadInvoices])
    usePolling(() => loadInvoices({ silent: true }), 20_000)

    const STATUS_LABEL = {
        paid: T.st_paid, unpaid: T.st_unpaid, overdue: T.st_overdue,
        issued: T.st_issued, draft: T.st_draft,
    }

    const filtered = items.filter(inv =>
        !search ||
        inv.number.toLowerCase().includes(search.toLowerCase()) ||
        (inv.buyer_name ?? '').toLowerCase().includes(search.toLowerCase())
    )

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead title={T.sales_title} sub={`${T.sales_sub_live} · ${MONTHS_PL[month - 1]} ${year}`} />

            <div className="toolbar">
                <div className="search">
                    <Icon name="search" size={14} />
                    <input
                        placeholder={T.search_placeholder}
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                    />
                    {search && (
                        <button className="btn-ghost" style={{ padding: '0 4px' }} onClick={() => setSearch('')}>
                            <Icon name="x" size={12} />
                        </button>
                    )}
                </div>
                <div className="filter-group">
                    <select className="filter-btn" value={month} onChange={e => setMonth(Number(e.target.value))} style={{ padding: '4px 8px' }}>
                        {MONTHS_PL.map((m, i) => <option key={i + 1} value={i + 1}>{m}</option>)}
                    </select>
                    <select className="filter-btn" value={year} onChange={e => setYear(Number(e.target.value))} style={{ padding: '4px 8px' }}>
                        {years.map(y => <option key={y} value={y}>{y}</option>)}
                    </select>
                </div>
                <button className="btn btn-ghost btn-sm" onClick={loadInvoices} title="Odśwież">
                    <Icon name="refresh" size={14} className={loading ? 'spinning' : ''} />
                </button>
            </div>

            {error && (
                <div className="card banner banner-err" style={{ padding: 16 }}>
                    {error}
                </div>
            )}

            <div className="card">
                <div style={{ overflowX: 'auto' }}>
                    <table className="files">
                        <thead>
                            <tr>
                                <th>{T.col_inv_no}</th>
                                <th>{T.col_inv_date}</th>
                                <th>{T.col_inv_buyer}</th>
                                <th style={{ textAlign: 'right' }}>{T.col_inv_net}</th>
                                <th style={{ textAlign: 'right' }}>{T.col_inv_vat}</th>
                                <th style={{ textAlign: 'right' }}>{T.col_inv_gross}</th>
                                <th>{T.col_inv_status}</th>
                            </tr>
                        </thead>
                        <tbody>
                            {loading && (
                                <tr><td colSpan={7} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-3)' }}>Ładowanie…</td></tr>
                            )}
                            {!loading && filtered.length === 0 && (
                                <tr><td colSpan={7} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-3)' }}>{T.empty}</td></tr>
                            )}
                            {!loading && filtered.map(inv => (
                                <tr key={inv.id}>
                                    <td><span className="mono" style={{ fontWeight: 500 }}>{inv.number}</span></td>
                                    <td className="mono dim">{fmtDate(inv.sell_date)}</td>
                                    <td>{inv.buyer_name ?? '—'}</td>
                                    <td className="mono" style={{ textAlign: 'right' }}>{fmtPLN(parseFloat(inv.price_net))}</td>
                                    <td className="mono dim" style={{ textAlign: 'right' }}>{fmtPLN(parseFloat(inv.price_tax))}</td>
                                    <td className="mono" style={{ textAlign: 'right', fontWeight: 500 }}>{fmtPLN(parseFloat(inv.price_gross))}</td>
                                    <td><Pill kind={STATUS_KIND[inv.status] ?? 'default'}>{STATUS_LABEL[inv.status] ?? inv.status ?? '—'}</Pill></td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
            {toast && <div className="toast">{toast}</div>}
        </div>
    )
}

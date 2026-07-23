import { useState, useEffect, useCallback } from 'react'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'
import { fmtPLN } from '../data'
import { getProducts } from '../api/endpoints'

export default function ProductsView() {
    const { getToken } = useAuth()
    const { t, lang } = useT()
    const T = t[lang]
    const [search, setSearch] = useState('')
    const [items, setItems] = useState([])
    const [loading, setLoading] = useState(false)
    const [activeOnly, setActiveOnly] = useState(false)
    const [toast, setToast] = useState(null)

    const showToast = msg => { setToast(msg); setTimeout(() => setToast(null), 3000) }

    const loadProducts = useCallback(async () => {
        setLoading(true)
        try {
            const token = await getToken()
            setItems(await getProducts({ activeOnly, token }))
        } catch (e) {
            showToast(`Błąd ładowania: ${e.message}`)
        } finally {
            setLoading(false)
        }
    }, [getToken, activeOnly])

    useEffect(() => {
        const timer = window.setTimeout(() => { void loadProducts() }, 0)
        return () => window.clearTimeout(timer)
    }, [loadProducts])

    const filtered = items.filter(p =>
        !search ||
        p.name.toLowerCase().includes(search.toLowerCase()) ||
        (p.code ?? '').toLowerCase().includes(search.toLowerCase())
    )

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead title={T.products_title} sub={T.products_sub} />

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
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, cursor: 'pointer' }}>
                    <input type="checkbox" checked={activeOnly} onChange={e => setActiveOnly(e.target.checked)} />
                    {T.products_active_only}
                </label>
                <button className="btn btn-ghost btn-sm" onClick={loadProducts} title="Odśwież">
                    <Icon name="refresh" size={14} className={loading ? 'spinning' : ''} />
                </button>
            </div>

            <div className="card">
                <div style={{ overflowX: 'auto' }}>
                    <table className="files">
                        <thead>
                            <tr>
                                <th>{T.col_sku}</th>
                                <th>{T.col_name2}</th>
                                <th style={{ textAlign: 'right' }}>{T.col_price}</th>
                                <th>{T.col_active}</th>
                            </tr>
                        </thead>
                        <tbody>
                            {loading && (
                                <tr><td colSpan={4} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-3)' }}>Ładowanie…</td></tr>
                            )}
                            {!loading && filtered.length === 0 && (
                                <tr><td colSpan={4} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-3)' }}>{T.empty}</td></tr>
                            )}
                            {!loading && filtered.map(p => (
                                <tr key={p.id}>
                                    <td><span className="mono" style={{ fontWeight: 500 }}>{p.code ?? '—'}</span></td>
                                    <td>{p.name}</td>
                                    <td className="mono" style={{ textAlign: 'right' }}>{fmtPLN(parseFloat(p.price_net))}</td>
                                    <td><Pill kind={p.active ? 'ok' : 'default'}>{p.active ? T.pill_ok : T.pill_inactive}</Pill></td>
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

import { useState } from 'react'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'
import { PRODUCTS, fmtPLN } from '../data'

export default function ProductsView() {
    const { t, lang } = useT()
    const T = t[lang]
    const [search, setSearch] = useState('')

    const items = PRODUCTS.filter(p =>
        !search || p.name.toLowerCase().includes(search.toLowerCase()) || p.sku.toLowerCase().includes(search.toLowerCase())
    )

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead title={T.products_title} sub={T.products_sub} />

            <div className="toolbar">
                <div className="search">
                    <Icon name="search" size={14} />
                    <input
                        placeholder="Szukaj produktu…"
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                    />
                    {search && (
                        <button className="btn-ghost" style={{ padding: '0 4px' }} onClick={() => setSearch('')}>
                            <Icon name="x" size={12} />
                        </button>
                    )}
                </div>
            </div>

            <div className="card">
                <div style={{ overflowX: 'auto' }}>
                    <table className="files">
                        <thead>
                            <tr>
                                <th>{T.col_sku}</th>
                                <th>{T.col_name2}</th>
                                <th>{T.col_capacity}</th>
                                <th>{T.col_pkg}</th>
                                <th style={{ textAlign: 'right' }}>{T.col_price}</th>
                                <th>{T.col_active}</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items.map(p => (
                                <tr key={p.sku}>
                                    <td><span className="mono" style={{ fontWeight: 500 }}>{p.sku}</span></td>
                                    <td>{p.name}</td>
                                    <td className="mono dim">{p.capacity}</td>
                                    <td className="dim">{p.pkg}</td>
                                    <td className="mono" style={{ textAlign: 'right' }}>{fmtPLN(p.price)}</td>
                                    <td><Pill kind={p.active ? 'ok' : 'default'}>{p.active ? T.pill_ok : 'nieaktywny'}</Pill></td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    )
}

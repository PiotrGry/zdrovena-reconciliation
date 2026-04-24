import { useState } from 'react'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'
import { ORDERS, fmtDate } from '../data'

const ORDER_STATUS_KIND = {
    new: 'info',
    prep: 'warn',
    sent: 'default',
    delivered: 'ok',
}

export default function OrdersView() {
    const { t, lang } = useT()
    const T = t[lang]
    const [search, setSearch] = useState('')

    const ORDER_STATUS_LABEL = {
        new: T.st_new,
        prep: T.st_prep,
        sent: T.st_sent,
        delivered: T.st_delivered,
    }

    const items = ORDERS.filter(o =>
        !search || o.id.toLowerCase().includes(search.toLowerCase()) || o.customer.toLowerCase().includes(search.toLowerCase())
    )

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead title={T.orders_title} sub={T.orders_sub} />

            <div className="toolbar">
                <div className="search">
                    <Icon name="search" size={14} />
                    <input
                        placeholder="Szukaj zamówienia…"
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
                                <th>{T.col_order_no}</th>
                                <th>{T.col_order_date}</th>
                                <th>{T.col_order_customer}</th>
                                <th style={{ textAlign: 'center' }}>{T.col_order_items}</th>
                                <th>{T.col_order_wz}</th>
                                <th>{T.col_order_status}</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items.map(o => (
                                <tr key={o.id}>
                                    <td><span className="mono" style={{ fontWeight: 500 }}>{o.id}</span></td>
                                    <td className="mono dim">{fmtDate(o.date)}</td>
                                    <td>{o.customer}</td>
                                    <td className="mono dim" style={{ textAlign: 'center' }}>{o.items}</td>
                                    <td><span className="mono dim">{o.wz}</span></td>
                                    <td><Pill kind={ORDER_STATUS_KIND[o.status] ?? 'default'}>{ORDER_STATUS_LABEL[o.status] ?? o.status}</Pill></td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    )
}

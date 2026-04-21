import { useState } from 'react'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'
import { SALES_INVOICES, fmtDate, fmtPLN } from '../data'

const STATUS_KIND = {
    paid: 'ok',
    unpaid: 'warn',
    overdue: 'err',
    issued: 'info',
    draft: 'default',
}

export default function SalesView() {
    const { t, lang } = useT()
    const T = t[lang]
    const [search, setSearch] = useState('')

    const STATUS_LABEL = {
        paid: T.st_paid,
        unpaid: T.st_unpaid,
        overdue: T.st_overdue,
        issued: T.st_issued,
        draft: T.st_draft,
    }

    const items = SALES_INVOICES.filter(inv =>
        !search || inv.id.toLowerCase().includes(search.toLowerCase()) || inv.buyer.toLowerCase().includes(search.toLowerCase())
    )

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead title={T.sales_title} sub={T.sales_sub} />

            <div className="toolbar">
                <div className="search">
                    <Icon name="search" size={14} />
                    <input
                        placeholder="Szukaj faktury…"
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
                            {items.map(inv => (
                                <tr key={inv.id}>
                                    <td><span className="mono" style={{ fontWeight: 500 }}>{inv.id}</span></td>
                                    <td className="mono dim">{fmtDate(inv.date)}</td>
                                    <td>{inv.buyer}</td>
                                    <td className="mono" style={{ textAlign: 'right' }}>{fmtPLN(inv.net)}</td>
                                    <td className="mono dim" style={{ textAlign: 'right' }}>{fmtPLN(inv.vat)}</td>
                                    <td className="mono" style={{ textAlign: 'right', fontWeight: 500 }}>{fmtPLN(inv.gross)}</td>
                                    <td><Pill kind={STATUS_KIND[inv.status] ?? 'default'}>{STATUS_LABEL[inv.status] ?? inv.status}</Pill></td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    )
}

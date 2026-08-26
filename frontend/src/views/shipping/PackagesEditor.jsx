import { useEffect, useState } from 'react'

import { BOX_STYLE, GLASS_TYPES, PACKAGE_TYPES } from './parcelTypes'

const HEAD_CELL = {
    textAlign: 'left',
    padding: '3px 12px 3px 0',
    fontSize: '11px',
    fontWeight: 600,
    color: 'var(--text-3)',
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
}

function plural(count) {
    if (count === 1) return 'paczka'
    const rest = count % 10
    const teens = count % 100
    return rest >= 2 && rest <= 4 && !(teens >= 12 && teens <= 14) ? 'paczki' : 'paczek'
}

function styleFor(type) {
    return GLASS_TYPES.has(type) ? BOX_STYLE.glass : BOX_STYLE.plastic
}

/**
 * The parcel plan is calculated from Shopify line items and used to be
 * read-only, so a mis-read product name could only be corrected with a deploy.
 * Editing closes once the shipment exists at the carrier — the API answers 409
 * there, and `canEdit` mirrors that rule.
 */
export function PackagesEditor({ breakdown, canEdit, onSave, saving = false }) {
    const [rows, setRows] = useState(() => (breakdown || []).map(row => ({ ...row })))

    // Keyed on the serialised plan, not the array identity: ShippingView polls
    // every 5s and hands back a fresh array each time, which would otherwise
    // wipe the operator's half-finished edit on every tick.
    const serverPlan = JSON.stringify(breakdown || [])
    useEffect(() => {
        setRows(JSON.parse(serverPlan))
    }, [serverPlan])

    const total = rows.reduce((sum, row) => sum + (Number(row.qty) || 0), 0)
    const dirty = JSON.stringify(rows) !== serverPlan
    const valid = rows.length > 0 && rows.every(row => Number(row.qty) >= 1 && Number(row.qty) <= 99)

    if (!canEdit) return <ReadOnlyTable rows={rows} total={total} />

    function updateRow(index, patch) {
        setRows(current => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))
    }

    function save() {
        // Errors surface as a toast through the view's withBusy wrapper, the
        // same way every other draft action reports failure.
        return onSave(rows.map(row => ({ type: row.type, qty: Number(row.qty) })))
    }

    return (
        <div>
            <div className="detail-label">Paczki</div>
            <table style={{ borderCollapse: 'collapse', width: '100%', marginTop: 6, fontSize: '0.9em' }}>
                <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)' }}>
                        <th style={HEAD_CELL}>Typ</th>
                        <th style={{ ...HEAD_CELL, textAlign: 'center' }}>Szt.</th>
                        <th style={HEAD_CELL}>Materiał</th>
                        <th style={HEAD_CELL} />
                    </tr>
                </thead>
                <tbody>
                    {rows.map((row, index) => (
                        <tr key={index} style={{ borderBottom: '1px solid var(--border)' }}>
                            <td style={{ padding: '6px 12px 6px 0' }}>
                                <select
                                    aria-label={`Typ paczki ${index + 1}`}
                                    value={row.type}
                                    onChange={e => updateRow(index, { type: e.target.value })}
                                    style={{ width: '100%' }}>
                                    {PACKAGE_TYPES.map(type => (
                                        <option key={type} value={type}>{type}</option>
                                    ))}
                                </select>
                            </td>
                            <td style={{ padding: '6px 12px', textAlign: 'center' }}>
                                <input
                                    aria-label={`Liczba sztuk ${index + 1}`}
                                    type="number"
                                    min="1"
                                    max="99"
                                    value={row.qty}
                                    onChange={e => updateRow(index, { qty: e.target.value })}
                                    style={{ width: 64, textAlign: 'center' }} />
                            </td>
                            <td style={{ padding: '6px 0', color: styleFor(row.type).color, fontWeight: 500 }}>
                                {GLASS_TYPES.has(row.type) ? 'szkło' : 'plastik'}
                            </td>
                            <td style={{ padding: '6px 0', textAlign: 'right' }}>
                                <button
                                    type="button"
                                    aria-label={`Usuń typ paczki ${index + 1}`}
                                    onClick={() => setRows(current => current.filter((_, i) => i !== index))}
                                    style={{ border: 0, background: 'none', cursor: 'pointer', color: 'var(--text-3)' }}>
                                    ×
                                </button>
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>

            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8, flexWrap: 'wrap' }}>
                <button
                    type="button"
                    onClick={() => setRows(current => [...current, { type: '1-pak', qty: 1 }])}>
                    Dodaj typ paczki
                </button>
                <button type="button" disabled={!valid || !dirty || saving} onClick={save}>
                    Zapisz paczki
                </button>
                <span className="dim" style={{ fontSize: '0.82em' }}>
                    {`Razem ${total} ${plural(total)} — tyle etykiet i numerów śledzenia`}
                </span>
            </div>
        </div>
    )
}

function ReadOnlyTable({ rows, total }) {
    if (!rows.length) {
        return (
            <div>
                <div className="detail-label">Paczki</div>
                <span className="dim">—</span>
            </div>
        )
    }
    return (
        <div>
            <div className="detail-label">Paczki</div>
            <table style={{ borderCollapse: 'collapse', width: '100%', marginTop: 6, fontSize: '0.9em' }}>
                <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)' }}>
                        <th style={HEAD_CELL}>Typ</th>
                        <th style={{ ...HEAD_CELL, textAlign: 'center' }}>Szt.</th>
                        <th style={HEAD_CELL}>Materiał</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map((row, index) => (
                        <tr key={index} style={{ borderBottom: '1px solid var(--border)' }}>
                            <td style={{ padding: '6px 12px 6px 0', fontWeight: 500 }}>
                                <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: styleFor(row.type).color, marginRight: 6, flexShrink: 0 }} />
                                {row.type}
                            </td>
                            <td style={{ padding: '6px 12px', textAlign: 'center' }}>
                                <span className="mono" style={{ fontWeight: 600 }}>{row.qty}</span>
                            </td>
                            <td style={{ padding: '6px 0', color: styleFor(row.type).color, fontWeight: 500 }}>
                                {GLASS_TYPES.has(row.type) ? 'szkło' : 'plastik'}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
            <div className="dim" style={{ fontSize: '0.82em', marginTop: 6 }}>
                {`Razem ${total} ${plural(total)} — tyle etykiet i numerów śledzenia`}
            </div>
        </div>
    )
}

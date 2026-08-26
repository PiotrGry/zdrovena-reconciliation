/**
 * Every parcel of a draft gets its own carrier shipment and its own tracking
 * number. The view used to render only draft.tracking_number — the first one —
 * so a three-parcel order looked like a one-parcel order.
 */
export function TrackingList({ draft }) {
    const shipments = draft.courier_shipments || []
    const numbered = shipments.filter(s => String(s.tracking_number || '').trim())

    if (!numbered.length) {
        return (
            <>
                <div className="detail-label">Numer śledzenia</div>
                <div>
                    {draft.tracking_number
                        ? <TrackingNumber value={draft.tracking_number} />
                        : <span className="dim">—</span>}
                </div>
            </>
        )
    }

    const pending = shipments.length - numbered.length
    const countByType = shipments.reduce((acc, s) => {
        acc[s.package_type] = (acc[s.package_type] || 0) + 1
        return acc
    }, {})

    return (
        <>
            <div className="detail-label">Numery śledzenia ({numbered.length})</div>
            <div style={{ display: 'grid', gap: 4 }}>
                {numbered.map(shipment => (
                    <div key={shipment.id || shipment.tracking_number}
                        style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                        <span className="dim" style={{ fontSize: '0.82em', minWidth: 86 }}>
                            {shipment.package_type} {shipment.package_number}/{countByType[shipment.package_type]}
                        </span>
                        <TrackingNumber value={shipment.tracking_number} />
                    </div>
                ))}
            </div>
            {pending > 0 && (
                <div className="dim" style={{ fontSize: '0.82em', marginTop: 4 }}>
                    {pending} z {shipments.length} paczek czeka na numer
                </div>
            )}
        </>
    )
}

function TrackingNumber({ value }) {
    return (
        <span className="mono copyable" title="Kliknij żeby skopiować"
            onClick={() => navigator.clipboard.writeText(value)}
            style={{ cursor: 'pointer' }}>
            {value}
        </span>
    )
}

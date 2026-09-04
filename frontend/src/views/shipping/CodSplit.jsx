const NOTE_STYLE = { marginTop: 4, fontSize: '0.82em', color: 'var(--warn, #b45309)' }
const ROW_STYLE = { display: 'flex', justifyContent: 'space-between', gap: 12 }

/**
 * What each parcel of a multi-parcel collect-on-delivery order collects.
 *
 * One parcel is one shipment with its own label and its own collection, so the
 * operator handing boxes to a courier needs to see the amounts before the
 * labels exist — not only inside the execution preview.
 *
 * The amounts come from the API, which computes them from the parcel plan. The
 * split is never recomputed here: two implementations of the same money would
 * eventually disagree, and the courier follows the one on the label.
 */
export function CodSplit({ draft }) {
    const cod = draft?.cod
    if (!cod) return null

    const amounts = draft.cod_split
    const error = draft.cod_split_error
    if (!amounts && !error) return null

    if (error) {
        return (
            <div style={{ marginTop: 10 }}>
                <div className="detail-label">Pobranie per paczka</div>
                <div style={{ ...NOTE_STYLE, color: 'var(--error)' }}>{error}</div>
            </div>
        )
    }

    return (
        <div style={{ marginTop: 10 }}>
            <div className="detail-label">Pobranie per paczka</div>
            {amounts.map((amount, index) => (
                <div key={index} style={ROW_STYLE}>
                    <span className="dim">Paczka {index + 1}</span>
                    <span className="mono">{amount} {cod.currency}</span>
                </div>
            ))}
            {draft.cod_split_basis === 'equal' && (
                <div style={NOTE_STYLE}>podział równy — brak cen pozycji</div>
            )}
        </div>
    )
}

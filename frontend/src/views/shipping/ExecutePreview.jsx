// What a draft will become, per provider.

import { Icon } from '../../components/Icon'

export function previewLine(label, value) {
    if (!value) return null
    return (
        <div style={{ display: 'flex', gap: 8, fontSize: '0.85em' }}>
            <span style={{ color: 'var(--text-2)', minWidth: 96 }}>{label}</span>
            <span style={{ fontWeight: 500 }}>{value}</span>
        </div>
    )
}

export function formatAddress(addr) {
    if (!addr) return ''
    const line = [addr.street, addr.building_number].filter(Boolean).join(' ')
    const city = [addr.post_code, addr.city].filter(Boolean).join(' ')
    return [line, city].filter(Boolean).join(', ')
}

/** Render a single ShipX parcel the way the courier will read it, not the way we stored it. */
/**
 * Apaczka's order shape is nothing like ShipX's, so read it on its own terms
 * rather than forcing one into the other. Reformatting either into a shared
 * intermediate would risk showing the operator something the courier never sees.
 */
export function ApaczkaPreviewParcel({ entry }) {
    const payload = entry.payload || {}
    const receiver = payload.address?.receiver || {}
    const box = (payload.shipment || [])[0] || {}
    const dimsText = box.dimension1
        ? `${box.dimension1} × ${box.dimension2} × ${box.dimension3} cm`
        : ''

    return (
        <div style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {previewLine('Usługa', payload.service_id ? `Apaczka #${payload.service_id}` : entry.service)}
            {previewLine('Referencja', payload.externalId || entry.reference)}
            {previewLine('Odbiorca', receiver.contact_person || receiver.name)}
            {previewLine('Adres', [receiver.line1, [receiver.postal_code, receiver.city].filter(Boolean).join(' ')].filter(Boolean).join(', '))}
            {previewLine('Telefon', receiver.phone)}
            {previewLine('Punkt odbioru', receiver.foreign_address_id)}
            {previewLine('Wymiary', dimsText)}
            {previewLine('Waga', box.weight != null ? `${box.weight} kg` : '')}
            {previewLine('Pobranie (COD)', payload.cod?.amount != null
                ? `${(Number(payload.cod.amount) / 100).toFixed(2)} ${payload.cod.currency}`
                : '')}
        </div>
    )
}

export function AllegroPreviewParcel({ entry }) {
    const payload = entry.payload || {}
    const receiver = payload.receiver || {}
    const box = (payload.packages || [])[0] || {}
    const dimsText = box.length
        ? `${box.length.value} × ${box.width?.value} × ${box.height?.value} cm`
        : ''

    return (
        <div style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {previewLine('Usługa', entry.service)}
            {previewLine('Zamówienie Allegro', payload.order_id || entry.reference)}
            {previewLine('Odbiorca', receiver.name)}
            {previewLine('Adres', [receiver.street, [receiver.postCode, receiver.city].filter(Boolean).join(' ')].filter(Boolean).join(', '))}
            {previewLine('Punkt odbioru', receiver.point)}
            {previewLine('Wymiary', dimsText)}
            {previewLine('Waga', box.weight?.value != null ? `${box.weight.value} kg` : '')}
        </div>
    )
}

export function ExecutePreviewParcel({ entry }) {
    const payload = entry.payload || {}
    // Three couriers, three unrelated payload shapes. Read each on its own
    // terms: a shared intermediate could show something no courier receives.
    if (payload.address) return <ApaczkaPreviewParcel entry={entry} />
    if (payload.packages) return <AllegroPreviewParcel entry={entry} />
    const parcel = (payload.parcels || [])[0] || {}
    const dims = parcel.dimensions
    // ShipX carries dimensions in mm; the operator thinks in cm, as the boxes are labelled.
    const dimsText = dims
        ? `${dims.length / 10} × ${dims.width / 10} × ${dims.height / 10} cm`
        : (parcel.template ? `szablon paczkomatu: ${parcel.template}` : '')
    const weight = parcel.weight?.amount
    const receiver = payload.receiver || {}
    const target = payload.custom_attributes?.target_point

    return (
        <div style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {previewLine('Usługa', payload.service || entry.service)}
            {previewLine('Referencja', payload.reference || entry.reference)}
            {previewLine('Odbiorca', [receiver.first_name, receiver.last_name].filter(Boolean).join(' '))}
            {previewLine('Adres', target ? `Paczkomat ${target}` : formatAddress(receiver.address))}
            {previewLine('Telefon', receiver.phone)}
            {previewLine('Wymiary', dimsText)}
            {previewLine('Waga', weight != null ? `${weight} kg` : '')}
            {previewLine('Pobranie (COD)', payload.cod?.amount != null
                ? `${Number(payload.cod.amount).toFixed(2)} ${payload.cod.currency}`
                : '')}
        </div>
    )
}

/**
 * The payload the courier is about to receive, shown before anything is sent.
 *
 * Read straight from the preview endpoint rather than re-derived from the draft:
 * a panel that reconstructed the payload itself could show something the courier
 * never sees, which is worse than showing nothing.
 */
export function ExecutePreview({ state }) {
    if (state.loading) return <div style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>Wczytywanie podglądu…</div>
    if (state.error) {
        return (
            <div className="error-banner">
                <Icon name="alertTriangle" size={13} />
                Nie udało się pobrać podglądu: {state.error}
            </div>
        )
    }
    const data = state.data || {}
    const parcels = data.parcels || []
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, maxWidth: 480 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {previewLine('Nadawca', data.sender?.name)}
                {previewLine('Adres nadania', formatAddress(data.sender))}
                {previewLine('Kurier', data.courier)}
            </div>
            {data.note && (
                <div style={{ fontSize: '0.8em', color: 'var(--text-2)' }}>{data.note}</div>
            )}
            {parcels.map(entry => (
                <ExecutePreviewParcel key={`${entry.package_type}-${entry.package_number}`} entry={entry} />
            ))}
        </div>
    )
}

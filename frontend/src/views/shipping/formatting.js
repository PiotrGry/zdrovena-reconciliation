// Pure presentation helpers for the shipping table.

import { packagesSortValue } from '../shippingTable'

export function fmtDate(iso) {
    if (!iso) return '—'
    try {
        return new Date(iso).toLocaleString('pl-PL', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        })
    } catch {
        return iso
    }
}

export function dayStamp() {
    // Matches zdrovena/shipping/domain/labels.py — the browser is already in
    // the operator's timezone, so no conversion is needed here.
    const now = new Date()
    return `${String(now.getDate()).padStart(2, '0')}.${String(now.getMonth() + 1).padStart(2, '0')}`
}

export function batchSheetTitle() {
    return `Etykiety portal ${dayStamp()}`
}

export function labelSheetTitle(orderNumber) {
    return `Etykieta ${String(orderNumber).replace(/^#/, '')} ${dayStamp()}`
}

export function printPdf(blob, title) {
    const url = URL.createObjectURL(blob)
    const frame = document.createElement('iframe')
    frame.title = title
    // Safari can print a blank PDF when its iframe is visibility:hidden. Keep
    // it renderable while placing it outside the visible viewport.
    frame.style.cssText = 'position:fixed;left:-10000px;top:-10000px;width:1px;height:1px;border:0'
    frame.src = url
    frame.onload = () => {
        frame.contentWindow?.focus()
        frame.contentWindow?.print()
    }
    document.body.appendChild(frame)
    window.setTimeout(() => {
        URL.revokeObjectURL(url)
        frame.remove()
    }, 60_000)
}

/**
 * Every carrier calls its pickup order something different and stores it in a
 * different shape. The operator quotes this id to that carrier's support when a
 * collection goes wrong, so all three have to surface in the same place.
 * Apaczka binds a pickup to a single order, so a multi-parcel draft can carry
 * several distinct numbers.
 */
export function pickupOrderIds(draft) {
    if (draft.courier === 'apaczka') {
        return (draft.courier_shipments || [])
            .map(shipment => String(shipment.pickup_number || '').trim())
            .filter(Boolean)
    }
    const single = draft.courier === 'allegro_delivery'
        ? draft.allegro_dispatch_id
        : draft.dispatch_order_id
    return String(single || '').trim() ? [String(single).trim()] : []
}

export function courierLabel(draft, apaczkaServices = []) {
    if (draft.courier === 'allegro_delivery') {
        if (draft.allegro_sending_method === 'parcel_locker') return 'Wysyłam z Allegro (Paczkomat)'
        if (draft.allegro_sending_method === 'dispatch_order') return 'Wysyłam z Allegro (Kurier InPost)'
        return 'Wysyłam z Allegro'
    }
    if (draft.courier === 'inpost') {
        if (draft.service === 'inpost_locker_standard') return 'InPost Paczkomat'
        if (draft.service === 'inpost_courier_standard') return 'InPost Kurier'
        return 'InPost'
    }
    if (draft.apaczka_service_id) {
        const match = apaczkaServices.find(s => s.service_id === draft.apaczka_service_id)
        if (match) return `Apaczka — ${match.label}`
    }
    return 'Apaczka'
}

export function courierPillKind(draft) {
    if (draft.courier === 'allegro_delivery') return 'warn'
    if (draft.courier === 'inpost') return 'info'
    return 'default'
}

export function matchStatusLabel(status) {
    switch (status) {
        case 'auto_matched':
            return 'Dopasowano automatycznie'
        case 'manual':
            return 'Wybrano ręcznie'
        case 'unrecognized':
            return 'Nie rozpoznano'
        case 'requires_selection':
            return 'Wymaga wyboru'
        default:
            return 'Wymaga wyboru'
    }
}

export function matchStatusPillKind(status) {
    if (status === 'auto_matched') return 'ok'
    if (status === 'manual') return 'info'
    return 'warn'
}

export function sourcePillKind(source) {
    if (source === 'allegro') return 'warn'
    if (source === 'shopify') return 'info'
    return 'default'
}

export function fmtOrderNum(num) {
    if (!num) return '—'
    const s = String(num)
    return `#${s}`
}

export function sortValue(draft, columnId, apaczkaServices = []) {
    switch (columnId) {
        case 'order':
            return draft.shopify_order_number
        case 'source':
            return draft.source || 'shopify'
        case 'customer':
            return draft.customer_name
        case 'packages':
            return packagesSortValue(draft)
        case 'courier':
            return courierLabel(draft, apaczkaServices)
        case 'date':
            return draft.order_date || draft.created_at
        case 'status':
            return draft.status
        default:
            return null
    }
}

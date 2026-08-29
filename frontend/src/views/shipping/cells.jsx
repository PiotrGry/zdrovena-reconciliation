// Small table cells with their own presentation rules.

import { Pill } from '../../components/Pill'
import { Icon } from '../../components/Icon'
import { fmtOrderNum, sourcePillKind } from './formatting'

export function OrderNumberCell({ draft }) {
    const orderNumber = draft.shopify_order_number
    if (!orderNumber) return <span className="mono">—</span>

    const value = String(orderNumber)
    const displayValue = fmtOrderNum(value)
    if (draft.source !== 'allegro') {
        return <span className="mono" title={value}>{displayValue}</span>
    }

    async function copyOrderNumber(event) {
        event.stopPropagation()
        await navigator.clipboard?.writeText(value)
    }

    return (
        <span className="order-id-cell" title={value}>
            <span className="mono order-id-full">{displayValue}</span>
            <button
                type="button"
                className="order-id-copy"
                onClick={copyOrderNumber}
                aria-label="Kopiuj pełne ID Allegro"
                title="Kopiuj pełne ID Allegro"
            >
                <Icon name="copy" size={12} />
            </button>
        </span>
    )
}

export function SourceCell({ source }) {
    const value = source || 'shopify'
    return <Pill kind={sourcePillKind(value)}>{value}</Pill>
}

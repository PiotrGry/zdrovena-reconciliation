import { useState } from 'react'

const WARN_STYLE = { marginTop: 4, fontSize: '0.82em', color: 'var(--warn, #b45309)' }

// Mirrors normalize_pl_phone in zdrovena/common/shipping_format.py. Used only to
// decide whether to show the warning — the API is the authority and re-validates
// every save, so a drift here costs a missing hint, never a bad shipment.
function looksUsable(value) {
    const digits = String(value || '').replace(/\D/g, '')
    return digits.length === 9 || (digits.startsWith('48') && digits.length === 11)
}

/**
 * InPost enforces a valid recipient phone from 2026-09-08. Before this field
 * existed the operator could only wave a phone-less draft through — the draft
 * PATCH had no phone parameter at all.
 */
export function RecipientPhone({ phone, canEdit, onSave, courier, saving = false }) {
    const stored = phone || ''

    // Adjusting state during render rather than in an effect: ShippingView polls
    // every 5s, and an effect keyed on the prop would wipe a half-typed number.
    const [value, setValue] = useState(stored)
    const [syncedPhone, setSyncedPhone] = useState(stored)
    if (stored !== syncedPhone) {
        setSyncedPhone(stored)
        setValue(stored)
    }

    const warn = courier === 'inpost' && !looksUsable(stored)

    if (!canEdit) {
        return (
            <>
                <div className="detail-label">Telefon odbiorcy</div>
                <div className="mono">{stored || <span className="dim">—</span>}</div>
                {warn && <div style={WARN_STYLE}>InPost wymaga telefonu odbiorcy</div>}
            </>
        )
    }

    return (
        <>
            <div className="detail-label">Telefon odbiorcy</div>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <input
                    aria-label="Telefon odbiorcy"
                    value={value}
                    onChange={e => setValue(e.target.value)}
                    placeholder="600100200"
                    style={{ width: 150 }} />
                <button
                    type="button"
                    disabled={saving || !value.trim() || value.trim() === stored}
                    onClick={() => onSave(value.trim())}>
                    Zapisz telefon
                </button>
            </div>
            {warn && <div style={WARN_STYLE}>InPost wymaga telefonu odbiorcy</div>}
        </>
    )
}

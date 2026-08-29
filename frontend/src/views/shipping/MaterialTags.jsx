// Package composition shown as chips.

import { BOX_STYLE } from './parcelTypes'

export const PACKAGES_LOCKED_STATUSES = new Set(['executing', 'pending_confirmation', 'created', 'cancelled'])

const _PACKAGE_UNITS = {
    '3-pak': { material: 'plastik', amount: 3 },
    '2-pak': { material: 'plastik', amount: 2 },
    '1-pak': { material: 'plastik', amount: 1 },
    'pół-pak': { material: 'plastik', amount: 0.5 },
    'szkło-2pak': { material: 'szkło', amount: 2 },
    'szkło': { material: 'szkło', amount: 1 },
}

export function materialTags(breakdown) {
    let plastic = 0, glass = 0
    for (const box of breakdown) {
        const packageInfo = _PACKAGE_UNITS[box.type]
        if (!packageInfo) continue
        const amount = packageInfo.amount * (box.qty ?? 1)
        if (packageInfo.material === 'szkło') glass += amount
        else plastic += amount
    }
    const tags = []
    if (plastic > 0) tags.push({ label: `plastik: ${String(plastic).replace('.', ',')} zgrzewki`, ...BOX_STYLE.plastic })
    if (glass > 0) tags.push({ label: `szkło: ${String(glass).replace('.', ',')} zgrzewki`, ...BOX_STYLE.glass })
    return tags
}

export function Chip({ label, style }) {
    return (
        <span style={{
            fontSize: '0.75em', padding: '1px 8px', borderRadius: 10,
            fontWeight: 500, whiteSpace: 'nowrap',
            background: style.bg, color: style.color, border: `1px solid ${style.border}`,
        }}>{label}</span>
    )
}


export function MaterialTags({ draft }) {
    const tags = materialTags(draft.packages_breakdown ?? [])
    return (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {tags.map(tag => (
                <Chip key={tag.label} label={tag.label} style={tag} />
            ))}
        </div>
    )
}

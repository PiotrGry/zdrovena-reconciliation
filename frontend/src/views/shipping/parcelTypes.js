/**
 * Two-zgrzewka glass packing is suspended, not deleted — mirrors
 * GLASS_2PAK_SUSPENDED in zdrovena/shipping/domain/planning.py, which carries
 * the full reason and the checklist for switching it back on. Both flags flip
 * together: this one hides the type from the operator, that one stops the
 * planner choosing it and expands stored rows into two boxes.
 */
export const GLASS_2PAK_SUSPENDED = true

const GLASS_2PAK = 'szkło-2pak'

/**
 * The types an operator may choose. A subset of PARCEL_SPECS in
 * zdrovena/common/shipping_parcels.py: the endpoint still accepts the
 * suspended "szkło-2pak" so a draft stored with it can be re-saved untouched,
 * but nothing may pick it anew while it is suspended.
 */
export const PACKAGE_TYPES = GLASS_2PAK_SUSPENDED
    ? ['3-pak', '2-pak', '1-pak', 'pół-pak', 'szkło']
    : ['3-pak', '2-pak', '1-pak', 'pół-pak', 'szkło', GLASS_2PAK]

/** Types kept readable on stored drafts but not offered for new plans. */
export const SUSPENDED_PACKAGE_TYPES = new Set(GLASS_2PAK_SUSPENDED ? [GLASS_2PAK] : [])

export const GLASS_TYPES = new Set(['szkło', GLASS_2PAK])

/**
 * How many physical parcels a plan produces — one label and one tracking
 * number each. While it is suspended, a "szkło-2pak" row counts as the two
 * boxes it always was, matching physical_parcels() on the server.
 */
export function parcelCount(rows) {
    return rows.reduce(
        (sum, row) => sum + (Number(row.qty) || 0) * (SUSPENDED_PACKAGE_TYPES.has(row.type) ? 2 : 1),
        0,
    )
}

/** Options for one row: the current choices, plus whatever this row already is. */
export function packageTypeOptions(currentType) {
    return PACKAGE_TYPES.includes(currentType) || !currentType
        ? PACKAGE_TYPES
        : [...PACKAGE_TYPES, currentType]
}

export const BOX_STYLE = {
    plastic: { color: '#3b82f6', bg: '#eff6ff', border: '#bfdbfe' },
    glass: { color: '#7c3aed', bg: '#f5f3ff', border: '#ddd6fe' },
}

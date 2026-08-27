/**
 * Mirrors PARCEL_SPECS in zdrovena/common/shipping_parcels.py. The PATCH
 * endpoint rejects anything outside this list, so the dropdown and the
 * server-side validator agree by construction.
 */
export const PACKAGE_TYPES = ['3-pak', '2-pak', '1-pak', 'pół-pak', 'szkło', 'szkło-2pak']

export const GLASS_TYPES = new Set(['szkło', 'szkło-2pak'])

export const BOX_STYLE = {
    plastic: { color: '#3b82f6', bg: '#eff6ff', border: '#bfdbfe' },
    glass: { color: '#7c3aed', bg: '#f5f3ff', border: '#ddd6fe' },
}

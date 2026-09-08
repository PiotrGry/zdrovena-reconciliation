import { describe, expect, it } from 'vitest'

import { materialTags, packagesLocked } from './MaterialTags'

describe('materialTags', () => {
    it('counts glass and plastic separately', () => {
        const tags = materialTags([{ type: '3-pak', qty: 1 }, { type: 'szkło', qty: 2 }])
        expect(tags.map(t => t.label)).toEqual(['plastik: 3 zgrzewki', 'szkło: 2 zgrzewki'])
    })

    it('counts a stored szkło-2pak row as the two boxes it is', () => {
        expect(materialTags([{ type: 'szkło-2pak', qty: 1 }])[0].label).toBe('szkło: 2 zgrzewki')
    })

    it('ignores a type it does not know', () => {
        expect(materialTags([{ type: 'karton', qty: 1 }])).toEqual([])
    })
})

describe('packagesLocked', () => {
    it.each(['executing', 'pending_confirmation', 'created', 'cancelled'])(
        'locks the plan in %s',
        status => {
            expect(packagesLocked({ status, courier_shipments: [] })).toBe(true)
        },
    )

    it('leaves a pending draft editable', () => {
        expect(packagesLocked({ status: 'pending', courier_shipments: [] })).toBe(false)
    })

    it('leaves a failed draft editable while nothing is booked', () => {
        // Most failures happen before any label exists. Repacking is how the
        // operator gets out of them, so it has to stay open.
        expect(packagesLocked({ status: 'error', courier_shipments: [] })).toBe(false)
    })

    it('locks a failed draft that already has a label at the carrier', () => {
        expect(packagesLocked({
            status: 'error',
            courier_shipments: [{ id: 'ship-1', package_type: '1-pak', package_number: '1' }],
        })).toBe(true)
    })

    it('survives a draft with no shipments field at all', () => {
        expect(packagesLocked({ status: 'pending' })).toBe(false)
    })
})

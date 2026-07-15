import { strict as assert } from 'node:assert'
import { describe, it } from 'node:test'

import {
    mergeColumnWidths,
    nextSortState,
    packagesSortValue,
    sortDrafts,
} from './shippingTable.js'

describe('shipping table sorting', () => {
    it('cycles from none to ascending, descending and none', () => {
        const asc = nextSortState({ key: null, direction: null }, 'customer')
        const desc = nextSortState(asc, 'customer')
        const none = nextSortState(desc, 'customer')

        assert.deepEqual(asc, { key: 'customer', direction: 'asc' })
        assert.deepEqual(desc, { key: 'customer', direction: 'desc' })
        assert.deepEqual(none, { key: null, direction: null })
    })

    it('keeps empty values last in both directions', () => {
        const drafts = [
            { id: 'empty', customer_name: '' },
            { id: 'zofia', customer_name: 'Zofia' },
            { id: 'adam', customer_name: 'Adam' },
        ]

        const ascending = sortDrafts(drafts, { key: 'customer', direction: 'asc' }, draft => draft.customer_name)
        const descending = sortDrafts(drafts, { key: 'customer', direction: 'desc' }, draft => draft.customer_name)

        assert.deepEqual(ascending.map(draft => draft.id), ['adam', 'zofia', 'empty'])
        assert.deepEqual(descending.map(draft => draft.id), ['zofia', 'adam', 'empty'])
    })

    it('sorts package counts from explicit count or breakdown', () => {
        const drafts = [
            { id: 'three', packages_breakdown: [{ qty: 1 }, { qty: 2 }] },
            { id: 'one', packages_count: 1 },
        ]

        const sorted = sortDrafts(drafts, { key: 'packages', direction: 'asc' }, packagesSortValue)

        assert.deepEqual(sorted.map(draft => draft.id), ['one', 'three'])
    })
})

describe('shipping table column widths', () => {
    it('merges saved widths without going below configured minimums', () => {
        const widths = mergeColumnWidths({ order: 20, customer: 260 })

        assert.equal(widths.order, 150)
        assert.equal(widths.customer, 260)
    })
})

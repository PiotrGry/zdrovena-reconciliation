import { describe, expect, it } from 'vitest'

import { rankByQuery } from './fuzzySearch'

function draft(overrides = {}) {
    return {
        id: 'draft-1',
        shopify_order_number: '1744',
        customer_name: 'Anna Nowak',
        courier: 'inpost',
        service: 'inpost_courier_standard',
        status: 'created',
        receiver: { first_name: 'Anna', last_name: 'Nowak', phone: '+48600111222' },
        shipping_address: { street: 'Prosta', city: 'Warszawa', post_code: '00-001' },
        courier_shipments: [
            { id: '2941156722', tracking_number: '523000015146050147436114', package_type: 'pół-pak' },
        ],
        dispatch_order_id: '16229861',
        ...overrides,
    }
}

const ids = results => results.map(record => record.id)

describe('rankByQuery', () => {
    it('finds a draft by a tracking number buried in courier_shipments', () => {
        const wanted = draft()
        const other = draft({ id: 'draft-2', courier_shipments: [], dispatch_order_id: null })

        expect(ids(rankByQuery([other, wanted], '523000015146050147436114'))).toEqual(['draft-1'])
    })

    it('finds a draft by the pickup order id', () => {
        const wanted = draft()
        const other = draft({ id: 'draft-2', dispatch_order_id: '99999999' })

        expect(ids(rankByQuery([other, wanted], '16229861'))).toEqual(['draft-1'])
    })

    it('ignores case and Polish diacritics', () => {
        const lodz = draft({ id: 'lodz', shipping_address: { city: 'Łódź' } })

        expect(ids(rankByQuery([lodz], 'lodz'))).toEqual(['lodz'])
        expect(ids(rankByQuery([lodz], 'ŁÓDŹ'))).toEqual(['lodz'])
    })

    it('matches a number the operator types with spaces against the stored digits', () => {
        const wanted = draft()

        expect(ids(rankByQuery([wanted], '600 111 222'))).toEqual(['draft-1'])
        expect(ids(rankByQuery([wanted], '5230 0001 5146'))).toEqual(['draft-1'])
    })

    it('matches a number typed without the separators the record stores', () => {
        const wanted = draft({
            id: 'formatted',
            receiver: { first_name: 'Anna', last_name: 'Nowak', phone: '+48 600 111 222' },
        })

        expect(ids(rankByQuery([wanted], '600111222'))).toEqual(['formatted'])
    })

    it('requires every whitespace-separated token to match somewhere', () => {
        const anna = draft({ id: 'anna' })
        const jan = draft({
            id: 'jan',
            customer_name: 'Jan Kowalski',
            receiver: { first_name: 'Jan', last_name: 'Kowalski' },
            shipping_address: { city: 'Kraków' },
        })

        expect(ids(rankByQuery([anna, jan], 'anna warszawa'))).toEqual(['anna'])
        expect(ids(rankByQuery([anna, jan], 'jan warszawa'))).toEqual([])
    })

    it('tolerates a dropped letter', () => {
        const wanted = draft({ id: 'warsaw' })

        expect(ids(rankByQuery([wanted], 'warszwa'))).toEqual(['warsaw'])
    })

    it('ranks a whole-field hit above a partial one', () => {
        const exact = draft({
            id: 'exact',
            customer_name: 'Nowak',
            receiver: { first_name: 'Anna', last_name: 'Nowak' },
        })
        const partial = draft({
            id: 'partial',
            customer_name: 'Anna Nowakowska',
            receiver: { first_name: 'Anna', last_name: 'Nowakowska' },
        })

        expect(ids(rankByQuery([partial, exact], 'nowak'))).toEqual(['exact', 'partial'])
    })

    it('does not match a query whose letters are merely scattered across a field', () => {
        const scattered = draft({
            id: 'scattered',
            customer_name: 'Natalia Ossowska Walkiewicz',
            receiver: { first_name: 'Natalia', last_name: 'Walkiewicz' },
        })

        expect(rankByQuery([scattered], 'nowak')).toEqual([])
    })

    it('returns every record in the original order for an empty query', () => {
        const first = draft({ id: 'first' })
        const second = draft({ id: 'second' })

        expect(ids(rankByQuery([first, second], '   '))).toEqual(['first', 'second'])
    })

    it('returns nothing for a query that matches no attribute', () => {
        expect(rankByQuery([draft()], 'qxzwv')).toEqual([])
    })

    it('does not let a two-letter typo query match everything', () => {
        const records = [draft({ id: 'a' }), draft({ id: 'b', customer_name: 'Zbigniew Ptak' })]

        expect(ids(rankByQuery(records, 'zb'))).toEqual(['b'])
    })
})

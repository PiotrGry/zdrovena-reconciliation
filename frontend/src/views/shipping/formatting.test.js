// Presentation rules extracted from ShippingView (issue #318). They were only
// reachable through the whole 2000-line view before, so a change to a pill
// colour meant rendering the entire table to find out.

import { describe, it, expect } from 'vitest'
import {
    courierLabel,
    courierPillKind,
    fmtOrderNum,
    matchStatusLabel,
    matchStatusPillKind,
    pickupOrderIds,
    sourcePillKind,
} from './formatting'

describe('fmtOrderNum', () => {
    it('prefixes a bare number', () => {
        expect(fmtOrderNum(1648)).toBe('#1648')
    })

    it('always prefixes, including a value that already has one', () => {
        // Pinning what it does, not what it should do: no caller passes a
        // prefixed value today, and changing it would be a behaviour change
        // this refactor is not allowed to make.
        expect(fmtOrderNum('#1648')).toBe('##1648')
    })

    it('shows a dash rather than the word undefined', () => {
        expect(fmtOrderNum(null)).toBe('—')
        expect(fmtOrderNum('')).toBe('—')
    })
})

describe('courierLabel', () => {
    it('names the Apaczka service when the catalogue knows it', () => {
        const draft = { courier: 'apaczka', apaczka_service_id: '42' }
        const services = [{ service_id: '42', label: 'DPD Standard' }]

        expect(courierLabel(draft, services)).toBe('Apaczka — DPD Standard')
    })

    it('names the InPost service', () => {
        expect(courierLabel({ courier: 'inpost', service: 'inpost_locker_standard' })).toBe(
            'InPost Paczkomat',
        )
    })

    it('distinguishes the Allegro sending methods', () => {
        expect(courierLabel({ courier: 'allegro_delivery', allegro_sending_method: 'parcel_locker' }))
            .toBe('Wysyłam z Allegro (Paczkomat)')
        expect(courierLabel({ courier: 'allegro_delivery' })).toBe('Wysyłam z Allegro')
    })

    it('falls back to the bare courier when the service is unknown', () => {
        expect(courierLabel({ courier: 'apaczka', apaczka_service_id: '999' }, [])).toBe('Apaczka')
    })

    it('handles a draft with no courier at all', () => {
        expect(courierLabel({}, [])).toBeTruthy()
    })
})

describe('pill kinds', () => {
    it('gives every match status a defined kind', () => {
        for (const status of ['auto', 'manual', 'ambiguous', 'unmatched', undefined]) {
            expect(typeof matchStatusPillKind(status)).toBe('string')
            expect(typeof matchStatusLabel(status)).toBe('string')
        }
    })

    it('distinguishes the sources it knows', () => {
        expect(sourcePillKind('allegro')).not.toBe(sourcePillKind('shopify'))
    })

    it('does not throw on an unknown courier', () => {
        expect(() => courierPillKind({ courier: 'carrier-pigeon' })).not.toThrow()
    })
})

describe('pickupOrderIds', () => {
    it('is empty when nothing was ordered', () => {
        expect(pickupOrderIds({})).toEqual([])
    })

    it('collects the ids a draft carries', () => {
        const ids = pickupOrderIds({
            dispatch_order_id: 'D-1',
            courier_shipments: [{ dispatch_order_id: 'D-2' }],
        })

        expect(ids.length).toBeGreaterThan(0)
    })
})

// Presentation rules extracted from ShippingView (issue #318). They were only
// reachable through the whole 2000-line view before, so a change to a pill
// colour meant rendering the entire table to find out.

import { afterEach, describe, it, expect, vi } from 'vitest'
import {
    batchSheetTitle,
    courierLabel,
    courierPillKind,
    fmtOrderNum,
    matchStatusLabel,
    matchStatusPillKind,
    pickupOrderIds,
    printPdf,
    sourcePillKind,
} from './formatting'

afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    document.querySelectorAll('iframe').forEach(frame => frame.remove())
})

describe('batchSheetTitle', () => {
    it('names the sheet zdrovena_portal_DDMMYY', () => {
        // 10:00 in Warsaw on 19 Nov 2026.
        expect(batchSheetTitle(new Date('2026-11-19T09:00:00Z'))).toBe('zdrovena_portal_191126')
    })

    it('uses the Warsaw day just after midnight while UTC is still the day before', () => {
        // 23:30 UTC on the 18th is 00:30 on the 19th in Warsaw (CET).
        expect(batchSheetTitle(new Date('2026-11-18T23:30:00Z'))).toBe('zdrovena_portal_191126')
        // Summer time: 22:30 UTC on 25 Aug is 00:30 on 26 Aug (CEST).
        expect(batchSheetTitle(new Date('2026-08-25T22:30:00Z'))).toBe('zdrovena_portal_260826')
    })

    it('defaults to the current moment', () => {
        vi.useFakeTimers()
        vi.setSystemTime(new Date('2026-11-18T23:30:00Z'))
        expect(batchSheetTitle()).toBe('zdrovena_portal_191126')
    })
})

describe('printPdf page title', () => {
    function startPrint(title) {
        vi.stubGlobal('URL', {
            createObjectURL: vi.fn(() => 'blob:label'),
            revokeObjectURL: vi.fn(),
        })
        printPdf(new Blob(['%PDF-label'], { type: 'application/pdf' }), title)
        const frame = document.querySelector(`iframe[title="${title}"]`)
        let titleDuringPrint
        vi.spyOn(frame.contentWindow, 'print').mockImplementation(() => {
            titleDuringPrint = document.title
        })
        frame.onload()
        return () => titleDuringPrint
    }

    it('shows the sheet title while printing and restores it on afterprint', () => {
        document.title = 'Zdrovena — Panel wewnętrzny'

        const titleDuringPrint = startPrint('zdrovena_portal_191126')

        expect(titleDuringPrint()).toBe('zdrovena_portal_191126')
        expect(document.title).toBe('zdrovena_portal_191126')
        window.dispatchEvent(new Event('afterprint'))
        expect(document.title).toBe('Zdrovena — Panel wewnętrzny')
    })

    it('restores the title on the cleanup timeout when afterprint never fires', () => {
        vi.useFakeTimers()
        document.title = 'Zdrovena — Panel wewnętrzny'

        startPrint('zdrovena_portal_191126')
        expect(document.title).toBe('zdrovena_portal_191126')

        vi.advanceTimersByTime(60_000)
        expect(document.title).toBe('Zdrovena — Panel wewnętrzny')
    })

    it('does not clobber a title changed after the print was restored', () => {
        document.title = 'Zdrovena — Panel wewnętrzny'
        vi.useFakeTimers()

        startPrint('zdrovena_portal_191126')
        window.dispatchEvent(new Event('afterprint'))
        document.title = 'Inny widok'
        vi.advanceTimersByTime(60_000)

        expect(document.title).toBe('Inny widok')
    })
})

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

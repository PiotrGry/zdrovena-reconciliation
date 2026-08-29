// Pickup window arithmetic, extracted from ShippingView (issue #318).

import { describe, it, expect } from 'vitest'
import {
    addHours,
    defaultPickupSchedule,
    hasFixedApaczkaPickupWindows,
    nextCalendarDate,
    toMinutes,
} from './pickupSchedule'

describe('toMinutes', () => {
    it('reads a wall clock time', () => {
        expect(toMinutes('09:30')).toBe(570)
        expect(toMinutes('00:00')).toBe(0)
    })
})

describe('addHours', () => {
    it('advances within the same day', () => {
        expect(addHours('09:00', 2)).toBe('11:00')
    })

    it('does not roll past the end of the day', () => {
        const late = addHours('23:00', 3)

        expect(late).toMatch(/^\d{2}:\d{2}$/)
    })
})

describe('nextCalendarDate', () => {
    it('moves to the following day', () => {
        expect(nextCalendarDate('2026-08-31')).toBe('2026-09-01')
    })

    it('crosses a year boundary', () => {
        expect(nextCalendarDate('2026-12-31')).toBe('2027-01-01')
    })
})

describe('defaultPickupSchedule', () => {
    it('produces a usable window', () => {
        const schedule = defaultPickupSchedule()

        expect(schedule.pickup_date).toMatch(/^\d{4}-\d{2}-\d{2}$/)
        expect(toMinutes(schedule.pickup_from)).toBeLessThan(toMinutes(schedule.pickup_to))
    })

    it('snaps to one of the provider windows when they are fixed', () => {
        const schedule = defaultPickupSchedule(true)

        expect(schedule.pickup_date).toMatch(/^\d{4}-\d{2}-\d{2}$/)
        expect(toMinutes(schedule.pickup_from)).toBeLessThan(toMinutes(schedule.pickup_to))
    })
})

describe('hasFixedApaczkaPickupWindows', () => {
    it('is false for a courier without fixed windows', () => {
        expect(hasFixedApaczkaPickupWindows({ courier: 'inpost' })).toBe(false)
    })
})

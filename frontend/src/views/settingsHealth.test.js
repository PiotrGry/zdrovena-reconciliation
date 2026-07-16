import { describe, expect, it } from 'vitest'

import {
    boolLabel,
    formatCheckedAt,
    statusKind,
    statusLabel,
} from './settingsHealth.js'

describe('settings integration health formatting', () => {
    it('maps backend health states to pill kinds', () => {
        expect(statusKind('healthy')).toBe('ok')
        expect(statusKind('unavailable')).toBe('err')
        expect(statusKind('degraded')).toBe('warn')
        expect(statusKind('not_configured')).toBe('warn')
    })

    it('maps backend health states to Polish labels', () => {
        expect(statusLabel('healthy')).toBe('Zdrowa')
        expect(statusLabel('degraded')).toBe('Ostrzeżenie')
        expect(statusLabel('unavailable')).toBe('Niedostępna')
        expect(statusLabel('not_configured')).toBe('Brak konfiguracji')
    })

    it('formats boolean environment fields', () => {
        expect(boolLabel(true)).toBe('tak')
        expect(boolLabel(false)).toBe('nie')
    })

    it('keeps invalid timestamps inspectable', () => {
        expect(formatCheckedAt('not-a-date')).toBe('not-a-date')
        expect(formatCheckedAt('')).toBe('—')
    })
})

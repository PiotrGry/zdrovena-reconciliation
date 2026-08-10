import { describe, expect, it } from 'vitest'

import { I18N } from './lang'

describe('shipping status translations', () => {
    it('describes pending confirmation independently from courier pickup', () => {
        expect(I18N.pl.sh_status_pending_confirmation).toBe('oczekuje na potwierdzenie')
        expect(I18N.en.sh_status_pending_confirmation).toBe('awaiting confirmation')
    })

    it('leaves the created shipment labels unchanged', () => {
        expect(I18N.pl.sh_status_created).toBe('nadane')
        expect(I18N.en.sh_status_created).toBe('created')
    })
})

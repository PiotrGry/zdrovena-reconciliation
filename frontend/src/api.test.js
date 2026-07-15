import { describe, expect, it, vi } from 'vitest'

import { fetchJson } from './api'
import { jsonResponse } from './test/http'

describe('fetchJson', () => {
    it('uses the Polish error envelope and correlation id without exposing backend details', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(jsonResponse({
            error_code: 'shipping_failed',
            message_pl: 'Nie udało się pobrać wysyłek.',
            correlation_id: 'corr-123',
            details: 'Traceback: SECRET_TOKEN=abc123',
        }, { status: 500 }))

        let error
        try {
            await fetchJson('/api/shipping/drafts')
        } catch (err) {
            error = err
        }

        expect(error).toMatchObject({
            message: 'Nie udało się pobrać wysyłek. (ID: corr-123)',
            code: 'shipping_failed',
            correlationId: 'corr-123',
            status: 500,
        })
        expect(error.message).not.toMatch(/SECRET_TOKEN|Traceback/)
    })
})

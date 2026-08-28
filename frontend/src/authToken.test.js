import { beforeEach, describe, expect, it, vi } from 'vitest'

import { acquireApiToken, resetInteractiveAcquisition } from './authToken'

const REQUEST = { scopes: ['api://x/user_access'] }

function msalWith({ silent, popup }) {
    return {
        acquireTokenSilent: vi.fn(silent),
        acquireTokenPopup: vi.fn(popup),
    }
}

beforeEach(() => {
    resetInteractiveAcquisition()
})

describe('acquireApiToken', () => {
    it('returns the silent token without any interaction', async () => {
        const msal = msalWith({
            silent: async () => ({ accessToken: 'silent-tok' }),
            popup: async () => ({ accessToken: 'popup-tok' }),
        })

        expect(await acquireApiToken(msal, REQUEST)).toBe('silent-tok')
        expect(msal.acquireTokenPopup).not.toHaveBeenCalled()
    })

    it('starts ONE interaction for concurrent callers', async () => {
        // The bug the operator hit: MSAL permits one interaction at a time, so
        // two callers each starting their own made the loser throw
        // interaction_in_progress.
        const msal = msalWith({
            silent: async () => {
                throw new Error('token expired')
            },
            popup: async () => {
                await new Promise(r => setTimeout(r, 20))
                return { accessToken: 'popup-tok' }
            },
        })

        const tokens = await Promise.all([
            acquireApiToken(msal, REQUEST),
            acquireApiToken(msal, REQUEST),
            acquireApiToken(msal, REQUEST),
        ])

        expect(msal.acquireTokenPopup).toHaveBeenCalledTimes(1)
        expect(tokens).toEqual(['popup-tok', 'popup-tok', 'popup-tok'])
    })

    it('a background caller never opens an interaction', async () => {
        // A timer carries no user gesture, so the browser would block the popup
        // anyway. Failing quietly is the honest outcome.
        const msal = msalWith({
            silent: async () => {
                throw new Error('token expired')
            },
            popup: async () => ({ accessToken: 'popup-tok' }),
        })

        await expect(
            acquireApiToken(msal, REQUEST, { interactive: false }),
        ).rejects.toThrow('token expired')
        expect(msal.acquireTokenPopup).not.toHaveBeenCalled()
    })

    it('lets a later caller retry after an interaction failed', async () => {
        // The lock must not wedge: a rejected interaction has to clear it, or
        // every later sign-in attempt inherits the same failure.
        let attempt = 0
        const msal = msalWith({
            silent: async () => {
                throw new Error('token expired')
            },
            popup: async () => {
                attempt += 1
                if (attempt === 1) throw new Error('user cancelled')
                return { accessToken: 'popup-tok' }
            },
        })

        await expect(acquireApiToken(msal, REQUEST)).rejects.toThrow('user cancelled')
        expect(await acquireApiToken(msal, REQUEST)).toBe('popup-tok')
        expect(msal.acquireTokenPopup).toHaveBeenCalledTimes(2)
    })

    it('shares the failure with everyone waiting on the same interaction', async () => {
        const msal = msalWith({
            silent: async () => {
                throw new Error('token expired')
            },
            popup: async () => {
                await new Promise(r => setTimeout(r, 10))
                throw new Error('user cancelled')
            },
        })

        const results = await Promise.allSettled([
            acquireApiToken(msal, REQUEST),
            acquireApiToken(msal, REQUEST),
        ])

        expect(msal.acquireTokenPopup).toHaveBeenCalledTimes(1)
        expect(results.every(r => r.status === 'rejected')).toBe(true)
    })
})

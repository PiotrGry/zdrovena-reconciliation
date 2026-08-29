// Guards the msal-browser API we actually call, against the real library.
//
// Every other auth test mocks MSAL, so they stay green even if a major bump
// removes a method we depend on — the mock still has it. This one imports the
// real package. Added while taking 3.30 → 5.19 (#207), where the whole question
// was whether our surface survived two majors.

import { describe, it, expect } from 'vitest'
import { PublicClientApplication, InteractionRequiredAuthError } from '@azure/msal-browser'

describe('msal-browser surface we depend on', () => {
    it('still exposes every method auth.jsx and authToken.js call', () => {
        const proto = PublicClientApplication.prototype
        for (const m of [
            'initialize', 'handleRedirectPromise', 'getAllAccounts',
            'loginRedirect', 'logoutRedirect', 'acquireTokenSilent', 'acquireTokenPopup',
        ]) {
            expect(typeof proto[m], m).toBe('function')
        }
        expect(typeof InteractionRequiredAuthError).toBe('function')
    })

    it('still accepts our configuration shape', () => {
        expect(() => new PublicClientApplication({
            auth: {
                clientId: '00000000-0000-0000-0000-000000000000',
                authority: 'https://login.microsoftonline.com/common',
                redirectUri: 'http://localhost',
            },
            cache: { cacheLocation: 'sessionStorage', storeAuthStateInCookie: false },
        })).not.toThrow()
    })
})

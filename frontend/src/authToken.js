/**
 * Serialised API-token acquisition.
 *
 * MSAL permits exactly one interactive operation at a time. `getToken` used to
 * fall from `acquireTokenSilent` straight into `acquireTokenPopup` with no
 * coordination, so every concurrent caller started its own interaction and the
 * losers threw `interaction_in_progress`.
 *
 * The Shipping view is where the operator saw it: it runs two independent
 * background timers (a 20s list poll and a 5s pending-confirmation poll) on top
 * of 17 token call sites, so the moment the cached token expires several
 * callers fail silently in the same tick.
 */

let interactiveAcquisition = null

/** Test seam — the module-level lock would otherwise leak between tests. */
export function resetInteractiveAcquisition() {
    interactiveAcquisition = null
}

/**
 * @param {object} msal            PublicClientApplication
 * @param {object} request         token request (scopes + account)
 * @param {boolean} opts.interactive  may this caller open a sign-in prompt?
 */
export async function acquireApiToken(msal, request, { interactive = true } = {}) {
    try {
        const silent = await msal.acquireTokenSilent(request)
        return silent.accessToken
    } catch (silentError) {
        // A background timer carries no user gesture, so a popup would be blocked
        // by the browser even if it won the race. Let the poll fail quietly; the
        // operator's next click does carry a gesture and refreshes the session
        // then.
        if (!interactive) throw silentError

        // One interaction, shared by everyone who wants it. Without this the
        // second caller is the one that reports interaction_in_progress.
        if (!interactiveAcquisition) {
            interactiveAcquisition = msal.acquireTokenPopup(request).finally(() => {
                interactiveAcquisition = null
            })
        }
        const result = await interactiveAcquisition
        return result.accessToken
    }
}

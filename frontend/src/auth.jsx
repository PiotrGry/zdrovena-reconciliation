import { PublicClientApplication } from '@azure/msal-browser'
import { createContext, useContext, useState, useEffect } from 'react'

const DEV_AUTH_DISABLED = import.meta.env.VITE_AUTH_DISABLED === 'true'

const API_CLIENT_ID = import.meta.env.VITE_AZURE_API_CLIENT_ID
const TENANT_ID = import.meta.env.VITE_AZURE_TENANT_ID

// Dev stub — mirrors backend _DEV_PRINCIPAL when VITE_AUTH_DISABLED=true
const DEV_ACCOUNT = {
    username: 'dev@local',
    name: 'Dev User',
    idTokenClaims: { roles: ['zdrovena-admin'] },
}

export const msalInstance = DEV_AUTH_DISABLED ? null : new PublicClientApplication({
    auth: {
        clientId: API_CLIENT_ID,
        authority: `https://login.microsoftonline.com/${TENANT_ID}`,
        redirectUri: window.location.origin,
    },
    cache: { cacheLocation: 'sessionStorage', storeAuthStateInCookie: false },
})

export const TOKEN_REQUEST = { scopes: [`api://${API_CLIENT_ID}/user_access`] }

export const AuthCtx = createContext(null)
export const useAuth = () => useContext(AuthCtx)

export function AuthProvider({ children }) {
    const [account, setAccount] = useState(undefined)
    const [roles, setRoles] = useState([])

    useEffect(() => {
        if (DEV_AUTH_DISABLED) {
            setAccount(DEV_ACCOUNT)
            setRoles(DEV_ACCOUNT.idTokenClaims.roles)
            return
        }
        msalInstance.initialize().then(() => {
            msalInstance.handleRedirectPromise()
                .then(resp => {
                    const acc = resp?.account || msalInstance.getAllAccounts()[0] || null
                    console.warn('[auth] handleRedirectPromise account:', acc?.username, 'roles:', acc?.idTokenClaims?.roles)
                    setAccount(acc)
                    if (acc) setRoles(acc.idTokenClaims?.roles || [])
                })
                .catch(err => {
                    console.error('[auth] handleRedirectPromise error:', err)
                    setAccount(null)
                })
        })
    }, [])

    const login = () => DEV_AUTH_DISABLED
        ? setAccount(DEV_ACCOUNT)
        : msalInstance.loginRedirect({ ...TOKEN_REQUEST, prompt: 'select_account' })

    const logout = () => DEV_AUTH_DISABLED
        ? setAccount(null)
        : msalInstance.logoutRedirect({ account })

    const getToken = async () => {
        if (DEV_AUTH_DISABLED) return 'dev-token'
        try {
            const r = await msalInstance.acquireTokenSilent({ ...TOKEN_REQUEST, account })
            return r.accessToken
        } catch {
            const r = await msalInstance.acquireTokenPopup({ ...TOKEN_REQUEST, account })
            return r.accessToken
        }
    }

    return (
        <AuthCtx.Provider value={{ account, roles, login, logout, getToken }}>
            {children}
        </AuthCtx.Provider>
    )
}

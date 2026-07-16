import { render } from '@testing-library/react'

import { AuthCtx } from '../auth'
import { I18N, LangCtx } from '../lang'
import { ToastProvider } from '../components/Toast'

export function renderWithProviders(
    ui,
    {
        auth = {
            account: { username: 'operator@example.com' },
            roles: ['zdrovena-admin', 'zdrovena-shipment-mgr'],
            getToken: async () => 'test-token',
            login: () => {},
            logout: () => {},
        },
        lang = 'pl',
    } = {}
) {
    return render(
        <LangCtx.Provider value={{ lang, t: I18N, setLang: () => {} }}>
            <AuthCtx.Provider value={auth}>
                <ToastProvider>
                    {ui}
                </ToastProvider>
            </AuthCtx.Provider>
        </LangCtx.Provider>
    )
}

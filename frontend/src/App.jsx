import { useCallback, useEffect, useState } from 'react'
import { useAuth } from './auth'
import { FEATURES } from './features'
import { LangCtx, I18N } from './lang'
import { Header } from './components/Header'
import Sidebar from './components/Sidebar'
import { ErrorBoundary } from './components/ErrorBoundary'
import FilesView from './views/FilesView'
import CloseView from './views/CloseView'
import SalesView from './views/SalesView'
import CostView from './views/CostView'
import ProductsView from './views/ProductsView'
import OrdersView from './views/OrdersView'
import UsersView from './views/UsersView'
import SettingsView from './views/SettingsView'
import ShippingView from './views/ShippingView'
import DlqView from './views/DlqView'
import DamageView from './views/DamageView'
import LoginScreen from './views/LoginScreen'
import { getDamageSummary } from './api/endpoints'
import { usePolling } from './hooks/usePolling'

const VIEWS = {
    files: FilesView,
    close: CloseView,
    sales: SalesView,
    costs: CostView,
    ...(FEATURES.products && { products: ProductsView }),
    ...(FEATURES.orders && { orders: OrdersView }),
    ...(FEATURES.users && { users: UsersView }),
    ...(FEATURES.shipping && { shipping: ShippingView }),
    ...(FEATURES.dlq && { dlq: DlqView }),
    ...(FEATURES.damage && { damage: DamageView }),
    settings: SettingsView,
}

function AppShell() {
    const [page, setPage] = useState(() => localStorage.getItem('zdrovena_page') || 'files')
    const [lang, setLang] = useState(() => localStorage.getItem('zdrovena_lang') || 'pl')
    const { getToken } = useAuth()
    const [damageCount, setDamageCount] = useState(0)

    const navigate = p => {
        setPage(p)
        localStorage.setItem('zdrovena_page', p)
    }

    const changeLang = l => {
        setLang(l)
        localStorage.setItem('zdrovena_lang', l)
    }

    const View = VIEWS[page] ?? FilesView

    const loadDamageCount = useCallback(async () => {
        if (!FEATURES.damage) return
        try {
            const token = await getToken()
            const summary = await getDamageSummary({ token })
            setDamageCount(summary.needs_review ?? 0)
        } catch {
            // A badge must never make the rest of the application unavailable.
        }
    }, [getToken])

    useEffect(() => {
        const timer = window.setTimeout(() => { void loadDamageCount() }, 0)
        return () => window.clearTimeout(timer)
    }, [loadDamageCount])
    usePolling(loadDamageCount, 30_000, { enabled: FEATURES.damage })

    return (
        <LangCtx.Provider value={{ lang, t: I18N, setLang: changeLang }}>
            <div className="app" data-density="roomy" data-sidebar="cream">
                <Header
                    damageCount={damageCount}
                    damageActive={page === 'damage'}
                    onDamageClick={() => navigate('damage')}
                />
                <Sidebar page={page} onNavigate={navigate} damageCount={damageCount} />
                <main className="main">
                    <ErrorBoundary resetKey={page}>
                        <View onNavigate={navigate} onDamageChanged={loadDamageCount} />
                    </ErrorBoundary>
                </main>
            </div>
        </LangCtx.Provider>
    )
}

export default function App() {
    const { account } = useAuth()
    const [lang, setLang] = useState(() => localStorage.getItem('zdrovena_lang') || 'pl')

    if (account === undefined) {
        return (
            <div className="loading-screen">
                <div className="spinner" />
            </div>
        )
    }

    if (account === null) {
        return (
            <LangCtx.Provider value={{ lang, t: I18N, setLang: l => { setLang(l); localStorage.setItem('zdrovena_lang', l) } }}>
                <LoginScreen />
            </LangCtx.Provider>
        )
    }

    return <AppShell />
}

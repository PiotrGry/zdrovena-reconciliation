import { useState } from 'react'
import { useAuth } from './auth'
import { FEATURES } from './features'
import { useT, LangCtx, I18N } from './lang'
import { Header } from './components/Header'
import Sidebar from './components/Sidebar'
import FilesView from './views/FilesView'
import CloseView from './views/CloseView'
import SalesView from './views/SalesView'
import CostView from './views/CostView'
import ProductsView from './views/ProductsView'
import OrdersView from './views/OrdersView'
import UsersView from './views/UsersView'
import SettingsView from './views/SettingsView'
import ShippingView from './views/ShippingView'
import LoginScreen from './views/LoginScreen'

const VIEWS = {
    files: FilesView,
    close: CloseView,
    sales: SalesView,
    costs: CostView,
    ...(FEATURES.products && { products: ProductsView }),
    ...(FEATURES.orders && { orders: OrdersView }),
    ...(FEATURES.users && { users: UsersView }),
    ...(FEATURES.shipping && { shipping: ShippingView }),
    settings: SettingsView,
}

function AppShell() {
    const [page, setPage] = useState(() => localStorage.getItem('zdrovena_page') || 'files')
    const [lang, setLang] = useState(() => localStorage.getItem('zdrovena_lang') || 'pl')
    const { t: _t } = useT()

    const navigate = p => {
        setPage(p)
        localStorage.setItem('zdrovena_page', p)
    }

    const changeLang = l => {
        setLang(l)
        localStorage.setItem('zdrovena_lang', l)
    }

    const View = VIEWS[page] ?? FilesView

    return (
        <LangCtx.Provider value={{ lang, t: I18N, setLang: changeLang }}>
            <div className="app" data-density="roomy" data-sidebar="cream">
                <Header />
                <Sidebar page={page} onNavigate={navigate} />
                <main className="main">
                    <View />
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

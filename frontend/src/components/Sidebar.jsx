import { useAuth } from '../auth'
import { useT } from '../lang'
import { Icon } from './Icon'
import { FEATURES } from '../features'

export function NavGroup({ label, children }) {
    return (
        <div className="nav-group">
            <div className="nav-title">{label}</div>
            {children}
        </div>
    )
}

export function NavItem({ iconName, label, page, current, onNavigate, badge }) {
    return (
        <button
            className={`nav-item${current === page ? ' active' : ''}`}
            onClick={() => onNavigate(page)}
        >
            <Icon name={iconName} size={16} className="icon" />
            {label}
            {badge != null && <span className="badge">{badge}</span>}
        </button>
    )
}

export default function Sidebar({ page, onNavigate }) {
    const { roles } = useAuth()
    const { t, lang } = useT()
    const T = t[lang]

    const canClose = roles.some(r => r === 'admin' || r === 'zdrovena-admin' || r === 'accountant' || r === 'zdrovena-accountant')

    return (
        <nav className="sidebar" aria-label="Główna nawigacja">
            <NavGroup label={T.nav_workspace}>
                <NavItem iconName="folder" label={T.nav_files} page="files" current={page} onNavigate={onNavigate} />
            </NavGroup>

            <NavGroup label={T.nav_accounting}>
                {canClose && (
                    <NavItem iconName="zap" label={T.nav_close} page="close" current={page} onNavigate={onNavigate} />
                )}
                {FEATURES.shipping && (
                    <NavItem iconName="truck" label={T.nav_shipping} page="shipping" current={page} onNavigate={onNavigate} />
                )}
                {FEATURES.dlq && (
                    <NavItem iconName="alertTriangle" label={T.nav_dlq} page="dlq" current={page} onNavigate={onNavigate} />
                )}
            </NavGroup>

            {(FEATURES.orders || FEATURES.products) && (
                <NavGroup label={T.nav_catalog}>
                    {FEATURES.orders && <NavItem iconName="chart" label={T.nav_orders} page="orders" current={page} onNavigate={onNavigate} />}
                    {FEATURES.products && <NavItem iconName="globe" label={T.nav_products} page="products" current={page} onNavigate={onNavigate} />}
                </NavGroup>
            )}

            <NavGroup label={T.nav_system}>
                {FEATURES.users && <NavItem iconName="users" label={T.nav_users} page="users" current={page} onNavigate={onNavigate} />}
                <NavItem iconName="settingsGear" label={T.nav_settings} page="settings" current={page} onNavigate={onNavigate} />
            </NavGroup>

            <div className="sidebar-footer">
                <div className="row">
                    <span><span className="status-dot" />{T.footer_health}</span>
                    <span>{T.footer_env}</span>
                </div>
                <div className="row"><span style={{ opacity: .6 }}>{T.footer_version}</span></div>
            </div>
        </nav>
    )
}

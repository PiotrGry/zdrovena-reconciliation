import { useAuth } from '../auth'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'

export default function UsersView() {
    const { account, roles } = useAuth()
    const { t, lang } = useT()
    const T = t[lang]

    const name = account?.name || account?.username || '—'
    const email = account?.username || '—'

    const ROLE_LABELS = {
        admin: T.role_admin,
        accountant: T.role_accountant,
        viewer: T.role_viewer,
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead title={T.users_title} sub={T.users_sub} />

            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="users" size={14} /> Twoje konto</span>
                </div>
                <div style={{ padding: '18px 20px', display: 'flex', alignItems: 'center', gap: 18 }}>
                    <div style={{
                        width: 52, height: 52, borderRadius: '50%',
                        background: 'linear-gradient(135deg, var(--primary), var(--accent))',
                        color: '#fff', fontSize: 18, fontWeight: 500, display: 'grid', placeItems: 'center',
                    }}>
                        {name.split(' ').map(p => p[0]).join('').slice(0, 2).toUpperCase()}
                    </div>
                    <div>
                        <div style={{ fontWeight: 500, fontSize: 15 }}>{name}</div>
                        <div style={{ color: 'var(--text-2)', fontSize: 13 }}>{email}</div>
                        <div style={{ marginTop: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                            {roles.length === 0
                                ? <Pill kind="warn">brak ról</Pill>
                                : roles.map(r => <Pill key={r} kind="info">{ROLE_LABELS[r] ?? r}</Pill>)
                            }
                        </div>
                    </div>
                </div>
            </div>

            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="shield" size={14} /> Role Entra ID</span>
                    <span className="card-sub">Enterprise app: zdrovena-api</span>
                </div>
                <table className="files">
                    <thead>
                        <tr>
                            <th>Rola</th>
                            <th>Uprawnienia</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {[
                            { role: 'admin', label: T.role_admin, perms: 'Pełny dostęp · zamykanie miesiąca · zarządzanie użytkownikami' },
                            { role: 'accountant', label: T.role_accountant, perms: 'Pobieranie i wgrywanie plików · zamykanie miesiąca' },
                            { role: 'viewer', label: T.role_viewer, perms: 'Tylko odczyt plików i raportów' },
                        ].map(row => (
                            <tr key={row.role}>
                                <td><span className="mono" style={{ fontWeight: 500 }}>{row.role}</span></td>
                                <td style={{ color: 'var(--text-2)', fontSize: 13 }}>{row.perms}</td>
                                <td>
                                    <Pill kind={roles.includes(row.role) ? 'ok' : 'default'}>
                                        {roles.includes(row.role) ? 'Przypisana' : 'Nie przypisana'}
                                    </Pill>
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    )
}

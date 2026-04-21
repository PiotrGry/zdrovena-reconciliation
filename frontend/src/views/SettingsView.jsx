import { useT } from '../lang'
import { useAuth } from '../auth'
import { PageHead } from '../components/PageHead'
import { Icon } from '../components/Icon'
import { Pill } from '../components/Pill'

const ROLE_LABELS = {
    'zdrovena-admin': 'Admin',
    'zdrovena-accountant': 'Księgowy',
    'zdrovena-viewer': 'Podgląd',
}

const INTEGRATIONS = [
    { name: 'Fakturownia', desc: 'Pobieranie raportów JPK_FA / JPK_V7M · Playwright (web UI)', icon: 'invoice', status: 'warn', detail: 'Brak API' },
    { name: 'KSeF MF', desc: 'Podpisywanie i wysyłanie JPK-V7M', icon: 'shield', status: 'ok', detail: 'Aktywna' },
    { name: 'Zoho Mail', desc: 'Pobieranie faktur kosztowych z e-maili · REST API OAuth 2.0', icon: 'bell', status: 'ok', detail: 'Aktywna' },
    { name: 'Azure Blob', desc: 'Przechowywanie plików zdrovena-docs', icon: 'cloud', status: 'ok', detail: 'Aktywna' },
    { name: 'Azure KeyVault', desc: 'Sekrety, certyfikaty, klucze', icon: 'key', status: 'ok', detail: 'Aktywna' },
]

export default function SettingsView() {
    const { t, lang } = useT()
    const T = t[lang]
    const { account, roles } = useAuth()

    const name = account?.name || account?.username || '—'
    const email = account?.username || '—'

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead title={T.settings_title} sub={T.settings_sub} />

            {/* Account */}
            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="users" size={14} /> Twoje konto Entra ID</span>
                </div>
                <div style={{ padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 12 }}>
                    <div style={{ display: 'grid', gridTemplateColumns: '140px 1fr', rowGap: 10, alignItems: 'center', fontSize: 13 }}>
                        <span style={{ color: 'var(--text-3)' }}>Nazwa</span>
                        <span style={{ fontWeight: 500 }}>{name}</span>
                        <span style={{ color: 'var(--text-3)' }}>E-mail / UPN</span>
                        <span className="mono">{email}</span>
                        <span style={{ color: 'var(--text-3)' }}>Role aplikacji</span>
                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                            {roles.length === 0
                                ? <Pill kind="warn">brak ról</Pill>
                                : roles.map(r => <Pill key={r} kind="info">{ROLE_LABELS[r] ?? r}</Pill>)
                            }
                        </div>
                    </div>
                </div>
            </div>

            {/* Organization */}
            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="globe" size={14} /> {T.s_org}</span>
                </div>
                <div className="two-col" style={{ padding: '20px 24px', alignItems: 'start' }}>
                    <div>
                        <p className="section-h">{T.s_org}</p>
                        <p className="section-sub">{T.s_org_sub}</p>
                    </div>
                    <div className="row-stack">
                        <div className="field">
                            <label>Nazwa firmy</label>
                            <input type="text" defaultValue="Zdrovena Sp. z o.o." readOnly />
                        </div>
                        <div className="field">
                            <label>NIP</label>
                            <input type="text" defaultValue="000-000-00-00" readOnly />
                        </div>
                    </div>
                </div>
            </div>

            {/* Integrations */}
            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="zap" size={14} /> {T.s_integr}</span>
                    <span className="card-sub">{T.s_integr_sub}</span>
                </div>
                {INTEGRATIONS.map(intg => (
                    <div key={intg.name} className="integr-row">
                        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                            <Icon name={intg.icon} size={16} style={{ color: 'var(--text-2)' }} />
                            <div>
                                <div style={{ fontWeight: 500, fontSize: 13 }}>{intg.name}</div>
                                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{intg.desc}</div>
                            </div>
                        </div>
                        <Pill kind={intg.status === 'ok' ? 'ok' : intg.status === 'warn' ? 'warn' : 'err'}>
                            {intg.detail}
                        </Pill>
                        <button className="btn btn-ghost btn-sm" title="Konfiguruj">
                            <Icon name="settingsGear" size={13} />
                        </button>
                    </div>
                ))}
            </div>

            {/* Secrets */}
            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="key" size={14} /> {T.s_secrets}</span>
                    <span className="card-sub">{T.s_secrets_sub}</span>
                </div>
                <div style={{ padding: '16px 20px', color: 'var(--text-3)', fontSize: 13 }}>
                    Sekrety przechowywane w Azure Key Vault. Dostęp przez Managed Identity (bez haseł w kodzie).
                </div>
            </div>
        </div>
    )
}

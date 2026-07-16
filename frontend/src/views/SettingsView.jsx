import { useCallback, useEffect, useState } from 'react'
import { useT } from '../lang'
import { useAuth } from '../auth'
import { fetchJson } from '../api'
import { PageHead } from '../components/PageHead'
import { Icon } from '../components/Icon'
import { Pill } from '../components/Pill'
import { boolLabel, formatCheckedAt, statusKind, statusLabel } from './settingsHealth'

const ROLE_LABELS = {
    'zdrovena-admin': 'Admin',
    'zdrovena-accountant': 'Księgowy',
    'zdrovena-viewer': 'Podgląd',
}

const INTEGRATION_ICONS = {
    auth: 'shield',
    storage: 'cloud',
    keyvault: 'key',
    shopify: 'bell',
    fakturownia: 'invoice',
    allegro: 'archive',
    inpost: 'truck',
    apaczka: 'truck',
}

export default function SettingsView() {
    const { t, lang } = useT()
    const T = t[lang]
    const { account, roles, getToken } = useAuth()
    const [health, setHealth] = useState(null)
    const [healthError, setHealthError] = useState('')
    const [healthLoading, setHealthLoading] = useState(true)

    const name = account?.name || account?.username || '—'
    const email = account?.username || '—'

    const isAdmin = roles.includes('zdrovena-admin')

    const loadHealth = useCallback(async ({ runChecks = false } = {}) => {
        setHealthLoading(true)
        setHealthError('')
        try {
            const token = await getToken()
            const suffix = runChecks ? '?run_checks=true' : ''
            const data = await fetchJson(`/api/integrations/health${suffix}`, { token })
            setHealth(data)
        } catch (err) {
            setHealthError(err.message || 'Nie udało się pobrać statusu integracji.')
        } finally {
            setHealthLoading(false)
        }
    }, [getToken])

    useEffect(() => {
        let alive = true
        async function loadOnce() {
            try {
                const token = await getToken()
                const data = await fetchJson('/api/integrations/health', { token })
                if (alive) setHealth(data)
            } catch (err) {
                if (alive) setHealthError(err.message || 'Nie udało się pobrać statusu integracji.')
            } finally {
                if (alive) setHealthLoading(false)
            }
        }
        loadOnce()
        return () => { alive = false }
    }, [getToken])

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
                    <div className="card-head-actions">
                        <span className="card-sub">
                            {health?.environment
                                ? `${T.s_integr_sub} · ${health.environment.app_env} · API ${health.environment.version}`
                                : T.s_integr_sub}
                        </span>
                        <button
                            className="btn btn-ghost btn-sm"
                            onClick={() => loadHealth({ runChecks: isAdmin })}
                            disabled={healthLoading}
                            title={isAdmin ? 'Sprawdź ponownie' : 'Odśwież'}
                        >
                            <Icon name="refresh" size={13} />
                        </button>
                    </div>
                </div>
                {health?.environment && (
                    <div className="env-health-grid">
                        <div className="env-health-item">
                            <span>Środowisko</span>
                            <strong>{health.environment.app_env}</strong>
                        </div>
                        <div className="env-health-item">
                            <span>Auth disabled</span>
                            <strong>{boolLabel(health.environment.auth_disabled)}</strong>
                        </div>
                        <div className="env-health-item">
                            <span>Key Vault</span>
                            <strong>{boolLabel(health.environment.keyvault_configured)}</strong>
                        </div>
                        <div className="env-health-item">
                            <span>Storage</span>
                            <strong>{health.environment.storage_backend}</strong>
                        </div>
                    </div>
                )}
                {healthLoading && (
                    <div className="integr-row">
                        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                            <Icon name="refresh" size={16} style={{ color: 'var(--text-2)' }} />
                            <div>
                                <div style={{ fontWeight: 500, fontSize: 13 }}>Status integracji</div>
                                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Pobieranie danych...</div>
                            </div>
                        </div>
                        <Pill kind="warn">Ładowanie</Pill>
                    </div>
                )}
                {!healthLoading && healthError && (
                    <div className="integr-row">
                        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                            <Icon name="alertTriangle" size={16} style={{ color: 'var(--danger)' }} />
                            <div>
                                <div style={{ fontWeight: 500, fontSize: 13 }}>Status integracji</div>
                                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{healthError}</div>
                            </div>
                        </div>
                        <Pill kind="err">Błąd</Pill>
                    </div>
                )}
                {!healthLoading && !healthError && health?.integrations?.map(intg => (
                    <div key={intg.key} className="integr-row">
                        <div className="integr-main">
                            <Icon name={INTEGRATION_ICONS[intg.key] || 'zap'} size={16} style={{ color: 'var(--text-2)' }} />
                            <div>
                                <div style={{ fontWeight: 500, fontSize: 13 }}>{intg.name}</div>
                                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{intg.message || intg.detail}</div>
                                <div className="integr-checks">
                                    {[
                                        intg.environment,
                                        intg.mode,
                                        intg.safe_operation,
                                        `${intg.latency_ms ?? 0} ms`,
                                        formatCheckedAt(intg.checked_at),
                                        intg.correlation_id ? `correlation: ${intg.correlation_id}` : '',
                                        ...(intg.checks || []),
                                    ].filter(Boolean).join(' · ')}
                                </div>
                            </div>
                        </div>
                        <Pill kind={statusKind(intg.status)}>
                            {statusLabel(intg.status)}
                        </Pill>
                    </div>
                ))}
            </div>

            {/* Operations */}
            {health?.operations?.length > 0 && (
                <div className="card">
                    <div className="card-head">
                        <span className="card-title"><Icon name="clock" size={14} /> Procesy operacyjne</span>
                        <span className="card-sub">ostatnie znane podsumowania</span>
                    </div>
                    {health.operations.map(op => (
                        <div key={op.key} className="integr-row">
                            <div className="integr-main">
                                <Icon name="clock" size={16} style={{ color: 'var(--text-2)' }} />
                                <div>
                                    <div style={{ fontWeight: 500, fontSize: 13 }}>{op.name}</div>
                                    <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{op.message}</div>
                                    <div className="integr-checks">
                                        {[formatCheckedAt(op.checked_at), ...Object.entries(op.metrics || {}).map(([k, v]) => `${k}: ${v ?? '—'}`)].join(' · ')}
                                    </div>
                                </div>
                            </div>
                            <Pill kind={statusKind(op.status)}>
                                {statusLabel(op.status)}
                            </Pill>
                        </div>
                    ))}
                </div>
            )}

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

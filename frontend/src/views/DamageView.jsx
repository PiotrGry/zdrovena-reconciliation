import { Fragment, useCallback, useEffect, useState } from 'react'
import { useAuth } from '../auth'
import { damageAction, getDamageCases, refreshDamageCases } from '../api/endpoints'
import { Icon } from '../components/Icon'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { useToast } from '../components/Toast'
import { usePolling } from '../hooks/usePolling'

const STATUS = {
    needs_review: ['warn', 'do sprawdzenia'],
    approved: ['info', 'potwierdzone'],
    replacement_prepared: ['info', 'draft gotowy'],
    replacement_pending: ['warn', 'tworzenie przesyłki'],
    replacement_created: ['ok', 'ponownie nadane'],
    customer_notified: ['ok', 'klient powiadomiony'],
    closed: ['default', 'zamknięte'],
    ignored: ['default', 'zignorowane'],
}

function fmtTs(value) {
    if (!value) return '—'
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString('pl-PL')
}

function actionCase(payload) {
    return payload?.case ?? payload
}

const EVIDENCE_LABELS = {
    source: 'Źródło',
    carrier_id: 'Przewoźnik',
    code: 'Kod zdarzenia',
    description: 'Opis',
    occurred_at: 'Czas zdarzenia',
    message_id: 'Identyfikator wiadomości',
    sender: 'Nadawca',
    subject: 'Temat',
    received_at: 'Odebrano',
    apaczka_order_id: 'Zlecenie Apaczka',
    apaczka_service: 'Usługa Apaczka',
    inpost_shipment_id: 'Przesyłka ShipX',
    inpost_service: 'Usługa InPost',
    provider_lookup_method: 'Pobrano z',
    correlation_method: 'Sposób powiązania',
    correlation_matched_fields: 'Zgodne dane',
    has_attachment: 'Załącznik',
}

const MATCH_LABELS = {
    email: 'e-mail',
    phone: 'telefon',
    name: 'imię i nazwisko',
    postal_code: 'kod pocztowy',
    city: 'miasto',
    street: 'adres',
    reference: 'numer zamówienia u przewoźnika',
}

function displayValue(key, value) {
    if (typeof value === 'boolean') return value ? 'tak' : 'nie'
    if (key.endsWith('_at')) return fmtTs(value)
    if (key === 'correlation_method' && value === 'inpost_tracking_lookup') return 'numer przesyłki → InPost'
    if (key === 'correlation_method' && value === 'apaczka_tracking_lookup') return 'numer przesyłki → Apaczka'
    if (key === 'provider_lookup_method' && value === 'inpost_tracking_lookup') return 'InPost ShipX po numerze przesyłki'
    if (key === 'provider_lookup_method' && value === 'apaczka_tracking_lookup') return 'Apaczka po numerze przesyłki'
    if (key === 'correlation_matched_fields' && Array.isArray(value)) {
        return value.map(field => MATCH_LABELS[field] ?? field).join(', ')
    }
    if (Array.isArray(value)) return value.join(', ')
    if (typeof value === 'object') {
        return Object.entries(value).map(([nestedKey, nestedValue]) => `${nestedKey}: ${nestedValue}`).join(', ')
    }
    return String(value)
}

function EvidenceCard({ evidence }) {
    const entries = Object.entries(evidence ?? {}).filter(([, value]) => (
        value !== null && value !== undefined && value !== ''
    ))
    const source = evidence?.source === 'zoho_inpost' ? 'Zoho · InPost'
        : evidence?.source === 'allegro_tracking' ? 'Allegro'
            : evidence?.source ?? 'Zdarzenie'
    return (
        <article className="damage-evidence-card">
            <div className="damage-evidence-head">
                <strong>{source}</strong>
                <span>{fmtTs(evidence?.occurred_at ?? evidence?.received_at)}</span>
            </div>
            {evidence?.description && <p className="damage-evidence-description">{evidence.description}</p>}
            <dl>
                {entries.filter(([key]) => !['source', 'description', 'occurred_at', 'received_at'].includes(key)).map(([key, value]) => (
                    <div key={key}>
                        <dt>{EVIDENCE_LABELS[key] ?? key.replaceAll('_', ' ')}</dt>
                        <dd className={key.endsWith('_id') || key === 'code' ? 'mono' : undefined}>{displayValue(key, value)}</dd>
                    </div>
                ))}
            </dl>
        </article>
    )
}

export default function DamageView({ onNavigate, onDamageChanged }) {
    const { getToken } = useAuth()
    const { pushToast } = useToast()
    const [cases, setCases] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)
    const [busy, setBusy] = useState(new Set())
    const [expanded, setExpanded] = useState(new Set())
    const [draftEdits, setDraftEdits] = useState({})

    const load = useCallback(async ({ silent = false } = {}) => {
        if (!silent) {
            setLoading(true)
            setError(null)
        }
        try {
            const token = await getToken()
            const data = await getDamageCases({ token })
            setCases(data.cases ?? [])
            onDamageChanged?.()
            if (silent) setError(null)
        } catch (e) {
            if (!silent) setError(e.message)
        } finally {
            if (!silent) setLoading(false)
        }
    }, [getToken, onDamageChanged])

    useEffect(() => { load() }, [load])
    usePolling(() => load({ silent: true }), 30_000)

    const replaceCase = updated => {
        if (!updated?.id) return
        setCases(items => items.map(item => item.id === updated.id ? updated : item))
        onDamageChanged?.()
    }

    const run = (item, action, { body, method, success } = {}) => async () => {
        setBusy(current => new Set([...current, item.id]))
        try {
            const token = await getToken()
            const payload = await damageAction({ id: item.id, action, token, body, method })
            replaceCase(actionCase(payload))
            if (success) pushToast({ kind: 'success', msg: success })
        } catch (e) {
            pushToast({ kind: 'error', msg: e.message })
        } finally {
            setBusy(current => {
                const next = new Set(current)
                next.delete(item.id)
                return next
            })
        }
    }

    const refresh = async () => {
        setLoading(true)
        try {
            const token = await getToken()
            await refreshDamageCases({ token })
            await load({ silent: true })
            pushToast({ kind: 'success', msg: 'Pobrano statusy Allegro i powiadomienia Zoho.' })
        } catch (e) {
            pushToast({ kind: 'error', msg: `Odświeżanie nie powiodło się: ${e.message}` })
        } finally {
            setLoading(false)
        }
    }

    const toggle = id => setExpanded(current => {
        const next = new Set(current)
        next.has(id) ? next.delete(id) : next.add(id)
        return next
    })

    const editDraft = (item, field, value) => setDraftEdits(current => ({
        ...current,
        [item.id]: {
            subject: current[item.id]?.subject ?? item.email_draft?.subject ?? '',
            body: current[item.id]?.body ?? item.email_draft?.body ?? '',
            [field]: value,
        },
    }))

    const controls = item => {
        const disabled = busy.has(item.id)
        if (item.status === 'needs_review') return (
            <>
                <button className="btn btn-primary btn-sm" disabled={disabled} onClick={run(item, 'confirm', { body: {}, success: 'Uszkodzenie potwierdzone.' })}>Potwierdź uszkodzenie</button>
                <button className="btn btn-ghost btn-sm" disabled={disabled} onClick={run(item, 'ignore', { success: 'Powiadomienie zignorowane.' })}>Ignoruj</button>
            </>
        )
        if (item.status === 'approved') return (
            <button className="btn btn-primary btn-sm" disabled={disabled} onClick={run(item, 'prepare-replacement', { success: 'Przygotowano draft nowej paczki — sprawdź dane przed nadaniem.' })}>Przygotuj nową paczkę</button>
        )
        if (item.status === 'replacement_prepared') return (
            <>
                <button className="btn btn-ghost btn-sm" onClick={() => onNavigate?.('shipping')}>Sprawdź w wysyłkach</button>
                <button className="btn btn-primary btn-sm" disabled={disabled} onClick={run(item, 'create-replacement', { success: 'Zlecono utworzenie nowej przesyłki.' })}>Nadaj nową paczkę</button>
            </>
        )
        if (item.status === 'replacement_pending') return (
            <button className="btn btn-primary btn-sm" disabled={disabled} onClick={run(item, 'confirm-replacement', { success: 'Sprawdzono status nowej przesyłki.' })}>Sprawdź status nadania</button>
        )
        if (item.status === 'replacement_created' && !item.email_draft) return (
            <>
                <button className="btn btn-primary btn-sm" disabled={disabled} onClick={run(item, 'email-draft', { success: 'Przygotowano wiadomość do klienta.' })}>Przygotuj e-mail</button>
                <button className="btn btn-ghost btn-sm" disabled={disabled} onClick={run(item, 'close', { success: 'Sprawa zamknięta bez wiadomości.' })}>Zamknij</button>
            </>
        )
        if (item.status === 'customer_notified') return (
            <button className="btn btn-primary btn-sm" disabled={disabled} onClick={run(item, 'close', { success: 'Sprawa zamknięta.' })}>Zamknij sprawę</button>
        )
        return null
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead
                title="Uszkodzone przesyłki"
                sub="Sygnały z Allegro i Zoho są tylko propozycją. Każdy etap wymaga osobnej decyzji operatora."
                actions={(
                    <button className="btn btn-ghost btn-sm" onClick={refresh} disabled={loading}>
                        <Icon name="refresh" size={14} className={loading ? 'spinning' : ''} />
                        Pobierz powiadomienia
                    </button>
                )}
            />

            {error && <div className="error-banner">Nie udało się wczytać uszkodzeń: {error}</div>}

            <div className="card">
                <div style={{ overflowX: 'auto' }}>
                    <table className="files">
                        <thead>
                            <tr>
                                <th>Wykryto</th>
                                <th>Przesyłka</th>
                                <th>Zamówienie / klient</th>
                                <th>Źródło</th>
                                <th>Status</th>
                                <th style={{ textAlign: 'right' }}>Akcje</th>
                            </tr>
                        </thead>
                        <tbody>
                            {loading && cases.length === 0 && <tr><td colSpan={6} style={{ textAlign: 'center', padding: 24 }}>Ładowanie…</td></tr>}
                            {!loading && !error && cases.length === 0 && <tr><td colSpan={6} style={{ textAlign: 'center', padding: 24, color: 'var(--text-3)' }}>Brak wykrytych uszkodzeń.</td></tr>}
                            {cases.map(item => {
                                const open = expanded.has(item.id)
                                const [pillKind, statusLabel] = STATUS[item.status] ?? ['default', item.status]
                                const edit = draftEdits[item.id] ?? item.email_draft ?? {}
                                return (
                                    <Fragment key={item.id}>
                                        <tr data-testid={`damage-row-${item.id}`}>
                                            <td className="mono">{fmtTs(item.detected_at)}</td>
                                            <td className="mono" style={{ fontWeight: 600 }}>{item.tracking_number || '—'}</td>
                                            <td>
                                                <div className="mono">{item.order_number || 'niepowiązane'}</div>
                                                <div style={{ color: 'var(--text-3)', fontSize: 12 }}>{item.customer_name || '—'}</div>
                                            </td>
                                            <td>{(item.sources ?? []).map(source => <Pill key={source}>{source.replace('_tracking', '').replace('_inpost', '')}</Pill>)}</td>
                                            <td><Pill kind={pillKind}>{statusLabel}</Pill></td>
                                            <td style={{ textAlign: 'right' }}>
                                                <div className="damage-actions">
                                                <button className="btn btn-ghost btn-sm" onClick={() => toggle(item.id)}>{open ? 'Ukryj' : 'Szczegóły'}</button>
                                                {controls(item)}
                                                </div>
                                            </td>
                                        </tr>
                                        {open && (
                                            <tr>
                                                <td colSpan={6} style={{ padding: 0 }}>
                                                    <div className="damage-details">
                                                        <section className="damage-panel">
                                                            <h3>Dowody i powiązanie</h3>
                                                            <div>Klasyfikacja: <strong>{item.classification}</strong> ({item.confidence})</div>
                                                            {item.correlation_method && (
                                                                <div>
                                                                    Powiązanie: <strong>{displayValue('correlation_method', item.correlation_method)}</strong>
                                                                    {item.correlation_matched_fields?.length > 0 && ` · ${displayValue('correlation_matched_fields', item.correlation_matched_fields)}`}
                                                                </div>
                                                            )}
                                                            {!item.correlation_method && item.provider_lookup_method && (
                                                                <div>Dane przesyłki: <strong>{displayValue('provider_lookup_method', item.provider_lookup_method)}</strong> · brak jednoznacznego zamówienia</div>
                                                            )}
                                                            <div>Oryginalny draft: <span className="mono">{item.shipping_draft_id || 'brak'}</span></div>
                                                            <div>Nowy draft: <span className="mono">{item.replacement_draft_id || 'jeszcze nie utworzono'}</span></div>
                                                            <div>Nowy tracking: <span className="mono">{item.replacement_tracking_number || 'jeszcze brak'}</span></div>
                                                            {(item.evidence ?? []).map((evidence, index) => (
                                                                <EvidenceCard evidence={evidence} key={`${item.id}-e-${index}`} />
                                                            ))}
                                                        </section>
                                                        <section className="damage-panel">
                                                            <h3>Wiadomość do klienta</h3>
                                                            {!item.email_draft && <div style={{ color: 'var(--text-3)' }}>Draft będzie dostępny dopiero po utworzeniu nowej przesyłki i nadaniu jej numeru śledzenia.</div>}
                                                            {item.email_draft && (
                                                                <>
                                                                    <div>Od: <strong>{item.email_draft.from}</strong></div>
                                                                    <div>Do: <strong>{item.email_draft.to}</strong></div>
                                                                    <input className="damage-email-field" aria-label="Temat wiadomości" value={edit.subject ?? ''} disabled={Boolean(item.email_sent_at)} onChange={event => editDraft(item, 'subject', event.target.value)} />
                                                                    <textarea className="damage-email-field" aria-label="Treść wiadomości" value={edit.body ?? ''} disabled={Boolean(item.email_sent_at)} onChange={event => editDraft(item, 'body', event.target.value)} />
                                                                    {!item.email_sent_at && (
                                                                        <div className="damage-email-actions">
                                                                            <button className="btn btn-ghost btn-sm" disabled={busy.has(item.id)} onClick={run(item, 'email-draft', { method: 'PATCH', body: { subject: edit.subject, body: edit.body }, success: 'Zapisano zmiany w wiadomości.' })}>Zapisz draft</button>
                                                                            <button className="btn btn-primary btn-sm" disabled={busy.has(item.id)} onClick={run(item, 'send-email', { success: 'Wiadomość została wysłana z info@wodahumio.pl.' })}>Wyślij e-mail</button>
                                                                        </div>
                                                                    )}
                                                                    {item.email_sent_at && <Pill kind="ok">wysłano {fmtTs(item.email_sent_at)}</Pill>}
                                                                </>
                                                            )}
                                                        </section>
                                                    </div>
                                                </td>
                                            </tr>
                                        )}
                                    </Fragment>
                                )
                            })}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    )
}

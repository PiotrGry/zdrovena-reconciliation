import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'
import { useToast } from '../components/Toast'
import { fetchJson } from '../api'
import { getShippingDrafts, syncShipping } from '../api/endpoints'
import { usePolling } from '../hooks/usePolling'
import {
    SHIPPING_COLUMNS,
    SHIPPING_TABLE_WIDTHS_KEY,
    loadColumnWidths,
    nextSortState,
    packagesSortValue,
    shippingGridTemplate,
    sortDrafts,
} from './shippingTable'

function fmtDate(iso) {
    if (!iso) return '—'
    try {
        return new Date(iso).toLocaleString('pl-PL', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        })
    } catch {
        return iso
    }
}

function courierLabel(draft, apaczkaServices = []) {
    if (draft.courier === 'allegro_delivery') {
        if (draft.allegro_sending_method === 'parcel_locker') return 'Wysyłam z Allegro (Paczkomat)'
        if (draft.allegro_sending_method === 'dispatch_order') return 'Wysyłam z Allegro (Kurier InPost)'
        return 'Wysyłam z Allegro'
    }
    if (draft.courier === 'inpost') {
        if (draft.service === 'inpost_locker_standard') return 'InPost Paczkomat'
        if (draft.service === 'inpost_courier_standard') return 'InPost Kurier'
        return 'InPost'
    }
    if (draft.apaczka_service_id) {
        const match = apaczkaServices.find(s => s.service_id === draft.apaczka_service_id)
        if (match) return `Apaczka — ${match.label}`
    }
    return 'Apaczka'
}

function courierPillKind(draft) {
    if (draft.courier === 'allegro_delivery') return 'warn'
    if (draft.courier === 'inpost') return 'info'
    return 'default'
}

function sourcePillKind(source) {
    if (source === 'allegro') return 'warn'
    if (source === 'shopify') return 'info'
    return 'default'
}

function fmtOrderNum(num) {
    if (!num) return '—'
    const s = String(num)
    return `#${s}`
}

function sortValue(draft, columnId, apaczkaServices = []) {
    switch (columnId) {
        case 'order':
            return draft.shopify_order_number
        case 'source':
            return draft.source || 'shopify'
        case 'customer':
            return draft.customer_name
        case 'packages':
            return packagesSortValue(draft)
        case 'courier':
            return courierLabel(draft, apaczkaServices)
        case 'date':
            return draft.order_date || draft.created_at
        case 'status':
            return draft.status
        default:
            return null
    }
}

function OrderNumberCell({ draft }) {
    const orderNumber = draft.shopify_order_number
    if (!orderNumber) return <span className="mono">—</span>

    const value = String(orderNumber)
    const displayValue = fmtOrderNum(value)
    if (draft.source !== 'allegro') {
        return <span className="mono" title={value}>{displayValue}</span>
    }

    async function copyOrderNumber(event) {
        event.stopPropagation()
        await navigator.clipboard?.writeText(value)
    }

    return (
        <span className="order-id-cell" title={value}>
            <span className="mono order-id-full">{displayValue}</span>
            <button
                type="button"
                className="order-id-copy"
                onClick={copyOrderNumber}
                aria-label="Kopiuj pełne ID Allegro"
                title="Kopiuj pełne ID Allegro"
            >
                <Icon name="copy" size={12} />
            </button>
        </span>
    )
}

function SourceCell({ source }) {
    const value = source || 'shopify'
    return <Pill kind={sourcePillKind(value)}>{value}</Pill>
}

function syncStat(result, key) {
    if (!result) return 0
    return ['allegro', 'shopify'].reduce((sum, source) => {
        const value = result[source]?.[key]
        return sum + (Number.isFinite(value) ? value : 0)
    }, 0)
}

function syncErrorCount(result) {
    if (!result) return 0
    return ['allegro', 'shopify'].reduce((sum, source) => {
        const sourceResult = result[source]
        if (!sourceResult) return sum
        return sum + (sourceResult.error ? 1 : 0) + (Number(sourceResult.errors) || 0)
    }, 0)
}

function syncSummary(result) {
    const created = syncStat(result, 'created')
    const updated = syncStat(result, 'updated')
    const unchanged = syncStat(result, 'unchanged') + syncStat(result, 'skipped') + syncStat(result, 'skipped_duplicate')
    const errors = syncErrorCount(result)
    return `Synchronizacja zakończona: ${created} nowe, ${updated} zaktualizowanych, ${unchanged} bez zmian, ${errors} błędów.`
}

function apiErrorMessage(body, response) {
    const message = body?.message_pl || body?.detail || `${response.status}`
    const correlationId = body?.correlation_id || response.headers?.get?.('X-Correlation-ID')
    return correlationId ? `${message} (ID: ${correlationId})` : message
}

function InvoicePreviewPanel({ draft, getToken, onClose, onCreated }) {
    const [loading, setLoading] = useState(true)
    const [creating, setCreating] = useState(false)
    const [preview, setPreview] = useState(null)
    const [error, setError] = useState(null)
    // R4.3: when the preview total does not match Allegro's "Do zapłaty", block
    // unsafe invoice creation until the operator explicitly acknowledges it.
    const [ackMismatch, setAckMismatch] = useState(false)

    useEffect(() => {
        // R4.3/#135: a fresh preview load (draft change OR reload) must clear any
        // prior mismatch acknowledgement — consent must never carry across drafts
        // or across preview versions of the same draft.
        setAckMismatch(false)
        setLoading(true)
        setError(null)
        const ctrl = new AbortController()
        getToken().then(token =>
            fetchJson(`/api/shipping/drafts/${draft.id}/invoice-preview`, {
                token,
                signal: ctrl.signal,
            })
        ).then(data => {
            if (!ctrl.signal.aborted) { setPreview(data); setLoading(false) }
        }).catch(e => {
            if (e.name !== 'AbortError' && !ctrl.signal.aborted) { setError(e.message); setLoading(false) }
        })
        return () => ctrl.abort()
    }, [draft.id, getToken])

    async function handleCreate() {
        setCreating(true)
        setError(null)
        try {
            const token = await getToken()
            const data = await fetchJson(`/api/shipping/drafts/${draft.id}/create-invoice`, {
                method: 'POST',
                token,
            })
            onCreated(data)
        } catch (e) {
            setError(e.message)
        } finally {
            setCreating(false)
        }
    }

    return createPortal(
        <div style={{ position: 'fixed', inset: 0, zIndex: 1000, display: 'flex' }}>
            <div onClick={onClose} style={{ flex: 1, background: 'rgba(0,0,0,0.35)' }} />
            <div style={{ width: 500, background: 'var(--bg, #fff)', boxShadow: '-4px 0 24px rgba(0,0,0,0.18)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <h3 style={{ margin: 0, fontSize: '1em', fontWeight: 600 }}>
                        <Icon name="invoice" size={15} style={{ marginRight: 6 }} />
                        Faktura — #{String(draft.shopify_order_number || '').slice(0, 12)}
                    </h3>
                    <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-2)', padding: 4, borderRadius: 4 }}>
                        <Icon name="x" size={18} />
                    </button>
                </div>

                <div style={{ flex: 1, overflow: 'auto', padding: 20 }}>
                    {loading && <div className="dim">Pobieranie danych z Allegro…</div>}
                    {error && <div className="error-banner" style={{ marginBottom: 12 }}><Icon name="alertTriangle" size={13} /> {error}</div>}
                    {preview?.status === 'already_created' && (
                        <div style={{ color: 'var(--ok, #16a34a)' }}>
                            <Icon name="check" size={14} /> Faktura już istnieje (ID: {preview.fakturownia_invoice_id})
                        </div>
                    )}
                    {preview?.status === 'preview_ready' && (
                        <>
                            <div style={{ marginBottom: 16 }}>
                                <div className="detail-label">Nabywca</div>
                                <div style={{ fontWeight: 500 }}>{preview.buyer_name}</div>
                                {preview.buyer_company && <div style={{ color: 'var(--text-2)' }}>{preview.buyer_company}</div>}
                                {preview.buyer_nip && <div className="mono dim" style={{ fontSize: '0.85em' }}>NIP: {preview.buyer_nip}</div>}
                                {preview.buyer_email && <div className="dim" style={{ fontSize: '0.85em' }}>{preview.buyer_email}</div>}
                            </div>
                            <div>
                                <div className="detail-label">Pozycje</div>
                                <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 6, fontSize: '0.88em' }}>
                                    <thead>
                                        <tr style={{ borderBottom: '2px solid var(--border)' }}>
                                            <th style={{ textAlign: 'left', padding: '3px 8px 3px 0', color: 'var(--text-3)', fontWeight: 600, fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Nazwa</th>
                                            <th style={{ textAlign: 'center', padding: '3px 8px', color: 'var(--text-3)', fontWeight: 600, fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Szt.</th>
                                            <th style={{ textAlign: 'right', padding: '3px 0 3px 8px', color: 'var(--text-3)', fontWeight: 600, fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Razem brutto</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {preview.positions.map((p, i) => (
                                            <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                                                <td style={{ padding: '6px 8px 6px 0' }}>
                                                    {p.name}
                                                    {p.vat_rate && <span className="dim" style={{ fontSize: '0.8em', marginLeft: 6 }}>VAT {p.vat_rate}</span>}
                                                </td>
                                                <td style={{ padding: '6px 8px', textAlign: 'center' }}>{p.quantity}</td>
                                                <td style={{ padding: '6px 0 6px 8px', textAlign: 'right', fontWeight: 500 }}>{p.line_total.toFixed(2)} zł</td>
                                            </tr>
                                        ))}
                                        {preview.settlement_positions.map((s, i) => (
                                            <tr key={`s${i}`} style={{ borderBottom: '1px solid var(--border)', color: 'var(--text-2)' }}>
                                                <td style={{ padding: '6px 8px 6px 0', fontStyle: 'italic' }}>{s.description}</td>
                                                <td />
                                                <td style={{ padding: '6px 0 6px 8px', textAlign: 'right' }}>{parseFloat(s.amount).toFixed(2)} zł</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                    <tfoot>
                                        <tr style={{ color: 'var(--text-2)', fontSize: '0.92em' }}>
                                            <td colSpan={2} style={{ padding: '8px 8px 2px 0' }}>Suma pozycji</td>
                                            <td style={{ padding: '8px 0 2px 8px', textAlign: 'right' }}>{(preview.positions_total ?? 0).toFixed(2)} zł</td>
                                        </tr>
                                        {(preview.settlement_total ?? 0) > 0 && (
                                            <tr style={{ color: 'var(--text-2)', fontSize: '0.92em' }}>
                                                <td colSpan={2} style={{ padding: '2px 8px 2px 0' }}>Kaucja za opakowania zwrotne</td>
                                                <td style={{ padding: '2px 0 2px 8px', textAlign: 'right' }}>{(preview.settlement_total ?? 0).toFixed(2)} zł</td>
                                            </tr>
                                        )}
                                        <tr>
                                            <td colSpan={2} style={{ padding: '8px 8px 4px 0', fontWeight: 700, fontSize: '1em', borderTop: '2px solid var(--border)' }}>Do zapłaty</td>
                                            <td style={{ padding: '8px 0 4px 8px', textAlign: 'right', fontWeight: 700, fontSize: '1em', borderTop: '2px solid var(--border)' }}>{preview.total_gross.toFixed(2)} zł</td>
                                        </tr>
                                    </tfoot>
                                </table>
                            </div>
                            {preview.allegro_total_to_pay != null && (
                                <div style={{ marginTop: 12, padding: '8px 12px', borderRadius: 6, fontSize: '0.88em',
                                    background: preview.matches_allegro ? 'var(--ok-bg, #f0fdf4)' : 'var(--warn-bg, #fffbeb)',
                                    border: `1px solid ${preview.matches_allegro ? 'var(--ok, #86efac)' : 'var(--warn, #fcd34d)'}` }}>
                                    {preview.matches_allegro
                                        ? <><Icon name="check" size={13} /> Zgadza się z Allegro „Do zapłaty” ({preview.allegro_total_to_pay.toFixed(2)} zł, bez dostawy)</>
                                        : <><Icon name="alertTriangle" size={13} /> Uwaga: różni się od Allegro „Do zapłaty” ({preview.allegro_total_to_pay.toFixed(2)} zł, bez dostawy){preview.difference != null && ` — różnica ${preview.difference > 0 ? '+' : ''}${preview.difference.toFixed(2)} zł`} — sprawdź przed wysłaniem</>
                                    }
                                </div>
                            )}
                            {preview.matches_allegro === false && (
                                <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, fontSize: '0.85em', color: 'var(--warn, #b45309)' }}>
                                    <input type="checkbox" checked={ackMismatch} onChange={e => setAckMismatch(e.target.checked)} />
                                    Rozumiem rozbieżność z Allegro i chcę mimo to utworzyć fakturę
                                </label>
                            )}
                        </>
                    )}
                </div>

                <div style={{ padding: '14px 20px', borderTop: '1px solid var(--border)', display: 'flex', gap: 8 }}>
                    {preview?.status === 'preview_ready' && (
                        <button className="btn btn-primary" onClick={handleCreate} disabled={creating || (preview.matches_allegro === false && !ackMismatch)}>
                            {creating
                                ? <><Icon name="loader" size={13} className="spin" /> Tworzenie…</>
                                : <><Icon name="invoice" size={13} /> Utwórz i załącz do Allegro</>
                            }
                        </button>
                    )}
                    <button className="btn btn-secondary" onClick={onClose}>Zamknij</button>
                </div>
            </div>
        </div>,
        document.body
    )
}

const _GLASS_TYPES = new Set(['szkło', 'szkło-2pak'])
const _BOX_STYLE = {
    plastic: { color: '#3b82f6', bg: '#eff6ff', border: '#bfdbfe' },
    glass:   { color: '#7c3aed', bg: '#f5f3ff', border: '#ddd6fe' },
}

function isGlassName(name) {
    return /szk[lł][eo]/i.test(name || '')
}

function materialTags(items) {
    let plastic = 0, glass = 0
    for (const it of items) {
        const qty = it.quantity ?? 1
        if (isGlassName(it.name)) glass += qty
        else plastic += qty
    }
    const tags = []
    if (plastic > 0) tags.push({ label: 'plastik', count: plastic, ..._BOX_STYLE.plastic })
    if (glass > 0) tags.push({ label: 'szkło', count: glass, ..._BOX_STYLE.glass })
    return tags
}

function Chip({ label, style }) {
    return (
        <span style={{
            fontSize: '0.75em', padding: '1px 8px', borderRadius: 10,
            fontWeight: 500, whiteSpace: 'nowrap',
            background: style.bg, color: style.color, border: `1px solid ${style.border}`,
        }}>{label}</span>
    )
}


function MaterialTags({ draft }) {
    const items = draft.order_items ?? []
    const tags = materialTags(items)
    return (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {tags.map(tag => (
                <Chip key={tag.label} label={`${tag.label} ×${tag.count}`} style={tag} />
            ))}
        </div>
    )
}

const TIME_SLOTS = ['07:00','08:00','09:00','10:00','11:00','12:00','13:00','14:00','15:00','16:00','17:00','18:00']

function toMinutes(t) { const [h, m] = t.split(':').map(Number); return h * 60 + m }
function addHours(t, hrs) {
    const m = toMinutes(t) + hrs * 60
    return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
}


function PickupScheduleModal({ onConfirm, onCancel, title }) {
    const { t, lang } = useT()
    const T = t[lang]
    const now = new Date()
    const today = now.toISOString().slice(0, 10)
    // Earliest allowed "from" on today: current hour + 2, rounded up to next slot
    const minFromToday = addHours(
        `${String(now.getHours()).padStart(2, '0')}:00`,
        2
    )

    const [date, setDate] = useState(today)
    const [from, setFrom] = useState(() => {
        const first = TIME_SLOTS.find(t => t >= minFromToday && t <= '16:00') || '09:00'
        return first
    })
    const [to, setTo] = useState(() => addHours(
        TIME_SLOTS.find(t => t >= minFromToday && t <= '16:00') || '09:00', 2
    ))

    const isToday = date === today
    const minFrom = isToday ? minFromToday : '07:00'

    function handleFromChange(val) {
        setFrom(val)
        if (toMinutes(to) < toMinutes(val) + 120) setTo(addHours(val, 2))
    }

    function handleDateChange(val) {
        setDate(val)
        // When switching to today, ensure from is still valid
        if (val === today && from < minFromToday) {
            const first = TIME_SLOTS.find(t => t >= minFromToday && t <= '16:00') || '09:00'
            setFrom(first)
            setTo(addHours(first, 2))
        }
    }

    const sel = { padding: '6px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.9em', cursor: 'pointer' }

    return createPortal(
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}
            onClick={e => { if (e.target === e.currentTarget) onCancel() }}>
            <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 24, minWidth: 320, display: 'flex', flexDirection: 'column', gap: 16 }}>
                <div style={{ fontWeight: 600 }}>{title}</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>{T.sh_pickup_date ?? 'Data podjazdu'}</label>
                    <input type="date" value={date} min={today}
                        onChange={e => { handleDateChange(e.target.value); e.target.blur() }}
                        style={sel}
                    />
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>{T.sh_time_from ?? 'Od'}</label>
                        <select value={from} onChange={e => handleFromChange(e.target.value)} style={sel}>
                            {TIME_SLOTS.filter(t => t >= minFrom && t <= '16:00').map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                    </div>
                    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>{T.sh_time_to ?? 'Do'}</label>
                        <select value={to} onChange={e => setTo(e.target.value)} style={sel}>
                            {TIME_SLOTS.filter(t => toMinutes(t) >= toMinutes(from) + 120).map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                    </div>
                </div>
                <div style={{ fontSize: '0.8em', color: 'var(--text-2)' }}>{T.sh_min_window ?? 'Minimalne okno: 2 godziny'}</div>
                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                    <button className="btn btn-ghost" onClick={onCancel}>{T.sh_cancel ?? 'Anuluj'}</button>
                    <button className="btn btn-primary"
                        onClick={() => onConfirm({ pickup_date: date, pickup_from: from, pickup_to: to })}>
                        {T.sh_confirm ?? 'Potwierdź'}
                    </button>
                </div>
            </div>
        </div>,
        document.body
    )
}

function DraftRow({ draft, onPrintLabel, onExecute, onPickup, onMarkFulfilled, onConfirmPending, onSetApaczkaService, onReviewDraft, apaczkaServices, busy, canManage, selected, onToggleSelect, forceOpen, getToken, onDraftUpdate, columnGridTemplate, tableMinWidth }) {
    const { t, lang } = useT()
    const T = t[lang]
    const [open, setOpen] = useState(false)
    const [selectedApaczkaService, setSelectedApaczkaService] = useState('')
    const [showInvoicePanel, setShowInvoicePanel] = useState(false)
    const [localInvoiceId, setLocalInvoiceId] = useState(draft.fakturownia_invoice_id || null)

    useEffect(() => {
        if (forceOpen !== undefined && forceOpen !== null) setOpen(forceOpen)
    }, [forceOpen])
    const [pickupModal, setPickupModal] = useState(null) // 'execute' | 'pickup' | null
    const isBusy = busy.has(draft.id)
    const needsPickupSchedule = draft.courier === 'inpost'
    const canPickup = (
        needsPickupSchedule &&
        draft.status === 'created' &&
        !draft.pickup_ordered
    )

    const isSelectable = onToggleSelect && (
        draft.status === 'pending' ||
        draft.status === 'needs_review' ||
        draft.status === 'error' ||
        (draft.courier === 'inpost' && draft.status === 'created' && !draft.pickup_ordered)
    )

    return (
        <div
            className={`accordion-row${open ? ' open' : ''}`}
            data-testid={`shipping-row-${draft.id}`}
            style={{ display: 'flex', alignItems: 'stretch', minWidth: tableMinWidth }}
        >
            <div style={{ width: 56, flexShrink: 0, display: 'flex', alignItems: open ? 'flex-start' : 'center', justifyContent: 'center', gap: 6, paddingTop: open ? 4 : 0 }}>
                {isSelectable ? (
                    <input
                        type="checkbox"
                        checked={selected || false}
                        onChange={() => onToggleSelect(draft.id)}
                        style={{ cursor: 'pointer', accentColor: 'var(--primary, #3b82f6)' }}
                    />
                ) : <span style={{ width: 16 }} />}
                <button
                    onClick={() => setOpen(o => !o)}
                    aria-expanded={open}
                    data-testid={`shipping-expand-${draft.id}`}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '6px 8px', color: 'var(--text-2)', display: 'flex', alignItems: 'center', borderRadius: 4 }}
                >
                    <Icon name={open ? 'chevronUp' : 'chevronDown'} size={20} />
                </button>
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
            <div
                className="accordion-header"
                style={{ padding: '10px 16px 10px 0', cursor: 'default', display: 'grid', alignItems: 'center',
                    gridTemplateColumns: columnGridTemplate }}
            >
                <OrderNumberCell draft={draft} />
                <span><SourceCell source={draft.source} /></span>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {draft.customer_name || '—'}
                </span>
                <span className="dim" style={{ fontSize: '0.8em', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {draft.receiver?.email || ''}
                </span>
                <span className="dim mono" style={{ fontSize: '0.8em', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {draft.receiver?.phone || ''}
                </span>
                <span style={{ display: 'flex', gap: 4, flexWrap: 'nowrap', overflow: 'hidden' }}><MaterialTags draft={draft} /></span>
                <span><Pill kind={courierPillKind(draft)}>{courierLabel(draft, apaczkaServices)}</Pill></span>
                <span className="mono dim" style={{ fontSize: '0.85em' }}>{fmtDate(draft.order_date || draft.created_at)}</span>
                <span>
                    <Pill kind={
                        draft.status === 'created' ? 'ok'
                            : draft.status === 'pending' ? 'default'
                            : draft.status === 'needs_review' ? 'warn'
                            : draft.status === 'pending_confirmation' ? 'info'
                            : 'warn'
                    }>
                        {draft.status === 'pending' ? (T.sh_status_pending ?? 'oczekujące')
                            : draft.status === 'created' ? (T.sh_status_created ?? 'nadane')
                            : draft.status === 'needs_review' ? (T.sh_status_needs_review ?? 'do sprawdzenia')
                            : draft.status === 'pending_confirmation' ? (T.sh_status_pending_confirmation ?? 'czeka na Allegro')
                            : (T.sh_status_error ?? 'błąd')}
                    </Pill>
                </span>
                <span>
                    {draft.pickup_ordered && (
                        <span style={{ fontSize: '0.72em', padding: '2px 7px', borderRadius: 4, background: 'var(--ok-subtle, #f0fdf4)', color: 'var(--ok, #16a34a)', border: '1px solid var(--ok-border, #86efac)', whiteSpace: 'nowrap' }}>
                            {T.sh_pickup_done ?? 'podjazd ✓'}
                        </span>
                    )}
                </span>
            </div>

            {open && (
                <div className="accordion-body">
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 2fr', gap: '12px 24px' }}>
                        <div>
                            <div className="detail-label">
                                {draft.service === 'inpost_locker_standard' ? 'Paczkomat' : 'Adres dostawy'}
                            </div>
                            {draft.service === 'inpost_locker_standard' ? (
                                <div>
                                    <span className="mono">{draft.receiver?.locker_id || '—'}</span>
                                    {draft.shipping_address?.city && (
                                        <span className="dim"> · {draft.shipping_address.city}</span>
                                    )}
                                </div>
                            ) : (
                                <div>
                                    {[draft.shipping_address?.street, draft.shipping_address?.building_number, draft.shipping_address?.flat_number].filter(Boolean).join(' ')}<br />
                                    {draft.shipping_address?.post_code} {draft.shipping_address?.city}
                                </div>
                            )}
                        </div>
                        <div>
                            <div className="detail-label">Numer śledzenia</div>
                            <div>
                                {draft.tracking_number
                                    ? (
                                        <span className="mono copyable" title="Kliknij żeby skopiować"
                                            onClick={() => navigator.clipboard.writeText(draft.tracking_number)}
                                            style={{ cursor: 'pointer' }}>
                                            {draft.tracking_number}
                                        </span>
                                    )
                                    : <span className="dim">—</span>}
                            </div>
                            <div className="detail-label" style={{ marginTop: 10 }}>ID draftu kuriera</div>
                            <div className="mono dim">{draft.courier_draft_id || '—'}</div>
                        </div>
                        <div>
                            <div className="detail-label">Paczki</div>
                            {draft.packages_breakdown?.length > 0 ? (
                                <table style={{ borderCollapse: 'collapse', width: '100%', marginTop: 6, fontSize: '0.9em' }}>
                                    <thead>
                                        <tr style={{ borderBottom: '1px solid var(--border)' }}>
                                            <th style={{ textAlign: 'left', padding: '3px 12px 3px 0', fontSize: '11px', fontWeight: 600, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Typ</th>
                                            <th style={{ textAlign: 'center', padding: '3px 12px', fontSize: '11px', fontWeight: 600, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Szt.</th>
                                            <th style={{ textAlign: 'left', padding: '3px 0', fontSize: '11px', fontWeight: 600, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Materiał</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {draft.packages_breakdown.map((b, i) => {
                                            const isGlass = _GLASS_TYPES.has(b.type)
                                            const s = isGlass ? _BOX_STYLE.glass : _BOX_STYLE.plastic
                                            return (
                                                <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                                                    <td style={{ padding: '6px 12px 6px 0', fontWeight: 500 }}>
                                                        <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: s.color, marginRight: 6, flexShrink: 0 }} />
                                                        {b.type}
                                                    </td>
                                                    <td style={{ padding: '6px 12px', textAlign: 'center' }}>
                                                        <span className="mono" style={{ fontWeight: 600, fontSize: '1em' }}>{b.qty}</span>
                                                    </td>
                                                    <td style={{ padding: '6px 0', color: s.color, fontWeight: 500 }}>
                                                        {isGlass ? 'szkło' : 'plastik'}
                                                    </td>
                                                </tr>
                                            )
                                        })}
                                    </tbody>
                                </table>
                            ) : <span className="dim">—</span>}
                        </div>
                    </div>

                    {draft.courier === 'apaczka' && (
                        <div style={{ marginTop: 12 }}>
                            <div className="detail-label">{T.sh_apaczka_service_label ?? 'Serwis Apaczka'}</div>
                            {draft.apaczka_service_id ? (
                                <div>{apaczkaServices.find(s => s.service_id === draft.apaczka_service_id)?.label || draft.apaczka_service_id}</div>
                            ) : (
                                <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
                                    <select
                                        value={selectedApaczkaService}
                                        onChange={e => setSelectedApaczkaService(e.target.value)}
                                        disabled={isBusy}
                                    >
                                        <option value="">{T.sh_apaczka_service_placeholder ?? '— wybierz serwis —'}</option>
                                        {apaczkaServices.map(s => (
                                            <option key={s.service_id} value={s.service_id}>{s.label}</option>
                                        ))}
                                    </select>
                                    <button
                                        className="btn btn-secondary"
                                        disabled={isBusy || !selectedApaczkaService}
                                        onClick={() => onSetApaczkaService(draft, selectedApaczkaService)}
                                    >
                                        {isBusy
                                            ? (T.sh_apaczka_service_save_busy ?? 'Zapisywanie…')
                                            : (T.sh_apaczka_service_save ?? 'Zapisz')}
                                    </button>
                                </div>
                            )}
                        </div>
                    )}

                    {draft.source === 'allegro' && (
                        <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
                            <div className="detail-label">Faktura Fakturownia</div>
                            {localInvoiceId ? (
                                <div style={{ marginTop: 4, color: 'var(--ok, #16a34a)', display: 'flex', alignItems: 'center', gap: 6 }}>
                                    <Icon name="check" size={14} />
                                    <span>Faktura #{localInvoiceId}</span>
                                </div>
                            ) : (
                                <div style={{ marginTop: 4, display: 'flex', alignItems: 'center', gap: 10 }}>
                                    <span className="dim" style={{ fontSize: '0.88em' }}>Brak faktury</span>
                                    {canManage && (
                            <button
                                className="btn btn-secondary"
                                data-testid={`shipping-invoice-${draft.id}`}
                                style={{ fontSize: '0.82em', padding: '3px 10px' }}
                                onClick={() => setShowInvoicePanel(true)}
                            >
                                            <Icon name="invoice" size={12} /> Podgląd i załącz
                                        </button>
                                    )}
                                </div>
                            )}
                        </div>
                    )}

                    {showInvoicePanel && (
                        <InvoicePreviewPanel
                            draft={draft}
                            getToken={getToken}
                            onClose={() => setShowInvoicePanel(false)}
                            onCreated={result => {
                                if (result.fakturownia_invoice_id) {
                                    setLocalInvoiceId(result.fakturownia_invoice_id)
                                    if (onDraftUpdate) onDraftUpdate()
                                }
                                setShowInvoicePanel(false)
                            }}
                        />
                    )}

                    {draft.error && (
                        <div className="error-banner" style={{ marginTop: 8 }}>
                            <Icon name="alertTriangle" size={13} />
                            {draft.error}
                        </div>
                    )}

                    <div className="draft-actions">
                        {canManage && (draft.status === 'pending' || draft.status === 'error') && (
                            <button
                                className="btn btn-primary"
                                data-testid={`shipping-execute-${draft.id}`}
                                onClick={() => needsPickupSchedule
                                    ? setPickupModal('execute')
                                    : onExecute(draft, null)
                                }
                                disabled={isBusy}
                            >
                                {isBusy
                                    ? <><Icon name="loader" size={13} className="spin" /> Realizowanie…</>
                                    : <><Icon name="send" size={13} /> Realizuj</>
                                }
                            </button>
                        )}

                        {canManage && draft.status === 'needs_review' && draft.courier !== 'apaczka' && (
                            <button
                                className="btn btn-primary"
                                onClick={() => onReviewDraft(draft)}
                                disabled={isBusy}
                            >
                                {isBusy
                                    ? <><Icon name="loader" size={13} className="spin" /> Zatwierdzanie…</>
                                    : <>Zatwierdź</>
                                }
                            </button>
                        )}

                        {draft.courier_draft_id && draft.status === 'created' && (
                            <button
                                className="btn btn-secondary"
                                onClick={() => onPrintLabel(draft)}
                                disabled={isBusy}
                            >
                                <Icon name="printer" size={13} />
                                Drukuj etykietę
                            </button>
                        )}

                        {draft.status === 'pending_confirmation' && (
                            <button
                                className="btn btn-secondary"
                                onClick={() => onConfirmPending(draft)}
                                disabled={isBusy}
                                title="Allegro jeszcze przetwarza tę przesyłkę — sprawdzane automatycznie co 5s, albo kliknij żeby sprawdzić od razu"
                            >
                                {isBusy
                                    ? <><Icon name="loader" size={13} className="spin" /> {T.sh_confirm_pending_busy ?? 'Sprawdzanie…'}</>
                                    : <><Icon name="refresh" size={13} /> {T.sh_confirm_pending ?? 'Sprawdź status'}</>
                                }
                            </button>
                        )}

                        {canManage && canPickup && (
                            <button
                                className="btn btn-secondary"
                                data-testid={`shipping-pickup-${draft.id}`}
                                onClick={() => setPickupModal('pickup')}
                                disabled={isBusy}
                            >
                                {isBusy
                                    ? <><Icon name="loader" size={13} className="spin" /> Zamawianie…</>
                                    : <><Icon name="truck" size={13} /> Zamów podjazd</>
                                }
                            </button>
                        )}

                        {draft.pickup_ordered && (
                            <span className="pickup-badge">
                                <Icon name="check" size={12} />
                                Podjazd zamówiony
                            </span>
                        )}

                        {canManage && draft.status === 'created' && (
                            draft.fulfillment_status === 'fulfilled' ? (
                                <span className="pickup-badge" title={draft.fulfilled_at || ''}>
                                    <Icon name="check" size={12} />
                                    Zrealizowane{draft.source === 'allegro' ? ' (Allegro: PROCESSING)' : ''}
                                </span>
                            ) : (
                                <button
                                    className="btn btn-secondary"
                                    onClick={() => onMarkFulfilled(draft)}
                                    disabled={isBusy}
                                    title={draft.source === 'allegro'
                                        ? 'Oznacz lokalnie jako zrealizowane i wyślij PROCESSING do Allegro'
                                        : 'Oznacz lokalnie jako zrealizowane'}
                                >
                                    {isBusy
                                        ? <><Icon name="loader" size={13} className="spin" /> Oznaczanie…</>
                                        : <><Icon name="check" size={13} /> Oznacz jako zrealizowane</>
                                    }
                                </button>
                            )
                        )}
                    </div>

                    {pickupModal && (
                        <PickupScheduleModal
                            title={pickupModal === 'execute' ? 'Zaplanuj podjazd kuriera' : 'Zamów podjazd kuriera'}
                            onCancel={() => setPickupModal(null)}
                            onConfirm={schedule => {
                                setPickupModal(null)
                                if (pickupModal === 'execute') onExecute(draft, schedule)
                                else onPickup(draft, schedule)
                            }}
                        />
                    )}
                </div>
            )}
            </div>
        </div>
    )
}

export default function ShippingView() {
    const { getToken, roles } = useAuth()
    const canManage = roles.includes('zdrovena-admin') || roles.includes('zdrovena-shipment-mgr')
    const { t, lang } = useT()
    const T = t[lang]
    const { pushToast } = useToast()

    const [drafts, setDrafts] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)
    const [search, setSearch] = useState('')
    const [filterStatus, setFilterStatus] = useState('all')
    const [filterCourier, setFilterCourier] = useState('all')
    const [filterSource, setFilterSource] = useState('all')
    const [filterDateFrom, setFilterDateFrom] = useState('')
    const [busy, setBusy] = useState(new Set())
    const [selectedDraftIds, setSelectedDraftIds] = useState(new Set())
    const [bulkProgress, setBulkProgress] = useState(null)
    const [bulkPickupModal, setBulkPickupModal] = useState(false)
    const [expandAll, setExpandAll] = useState(null)
    const [apaczkaServices, setApaczkaServices] = useState([])
    const [syncing, setSyncing] = useState(false)
    const [syncResult, setSyncResult] = useState(null)
    const [columnWidths, setColumnWidths] = useState(loadColumnWidths)
    const [sortState, setSortState] = useState({ key: null, direction: null })
    const resizeRef = useRef(null)

    const columnGridTemplate = useMemo(
        () => shippingGridTemplate(columnWidths),
        [columnWidths]
    )
    const tableMinWidth = useMemo(
        () => 56 + 16 + ((SHIPPING_COLUMNS.length - 1) * 12) +
            SHIPPING_COLUMNS.reduce((sum, column) => sum + (columnWidths[column.id] || column.width), 0),
        [columnWidths]
    )

    useEffect(() => {
        window.localStorage.setItem(SHIPPING_TABLE_WIDTHS_KEY, JSON.stringify(columnWidths))
    }, [columnWidths])

    function handleSort(column) {
        if (!column.sortable) return
        setSortState(current => nextSortState(current, column.id))
    }

    function startColumnResize(event, column) {
        event.preventDefault()
        event.stopPropagation()
        resizeRef.current = {
            columnId: column.id,
            startX: event.clientX,
            startWidth: columnWidths[column.id] || column.width,
            minWidth: column.minWidth,
        }

        function onPointerMove(moveEvent) {
            const resize = resizeRef.current
            if (!resize) return
            const nextWidth = Math.max(resize.minWidth, resize.startWidth + moveEvent.clientX - resize.startX)
            setColumnWidths(widths => ({ ...widths, [resize.columnId]: nextWidth }))
        }

        function onPointerUp() {
            resizeRef.current = null
            document.removeEventListener('pointermove', onPointerMove)
            document.removeEventListener('pointerup', onPointerUp)
        }

        document.addEventListener('pointermove', onPointerMove)
        document.addEventListener('pointerup', onPointerUp)
    }

    // silent=true dla odświeżania w tle (polling): nie miga spinnerem i nie
    // podmienia listy na komunikat błędu — zostawia ostatnie dobre dane.
    const load = useCallback(async ({ silent = false } = {}) => {
        if (!silent) {
            setLoading(true)
            setError(null)
        }
        try {
            const token = await getToken()
            const data = await getShippingDrafts({ token })
            setDrafts(data.drafts ?? [])
            if (silent) setError(null)
        } catch (e) {
            if (!silent) setError(e.message)
        } finally {
            if (!silent) setLoading(false)
        }
    }, [getToken])

    const handleSync = useCallback(async () => {
        setSyncing(true)
        setSyncResult(null)
        try {
            const token = await getToken()
            const body = await syncShipping({ token })
            setSyncResult(body)
            await load()
            const summary = syncSummary(body)
            pushToast({
                kind: syncErrorCount(body) > 0 ? 'error' : 'success',
                msg: summary,
                sticky: syncErrorCount(body) > 0,
            })
        } catch (e) {
            setSyncResult({ error: e.message })
            pushToast({ kind: 'error', msg: `Synchronizacja nie powiodła się: ${e.message}` })
        } finally {
            setSyncing(false)
        }
    }, [getToken, load, pushToast])

    useEffect(() => { load() }, [load])

    // Reaktywność: nowe drafty z webhooków Shopify / pollera Allegro pojawiają się
    // w ≤20 s bez F5. Visibility-aware — nie odpytuje, gdy karta jest w tle.
    usePolling(() => load({ silent: true }), 20_000)

    useEffect(() => {
        let cancelled = false
        async function loadApaczkaServices() {
            try {
                const token = await getToken()
                const res = await fetch('/api/shipping/apaczka-services', {
                    headers: { Authorization: `Bearer ${token}` },
                })
                if (res.ok) {
                    const body = await res.json()
                    if (!cancelled) setApaczkaServices(body.services || [])
                }
            } catch {
                // Non-critical: dropdown stays empty; PATCH still works via
                // curl/Postman with a known service_id if this fetch fails.
            }
        }
        loadApaczkaServices()
        return () => { cancelled = true }
    }, [getToken])

    function withBusy(draftId, fn, actionLabel) {
        return async () => {
            setBusy(s => new Set([...s, draftId]))
            try {
                await fn()
                await load()
            } catch (e) {
                const prefix = actionLabel ? `${actionLabel}: ` : ''
                pushToast({ kind: 'error', msg: `${prefix}${e.message || 'nieznany błąd'}` })
            } finally {
                setBusy(s => { const n = new Set(s); n.delete(draftId); return n })
            }
        }
    }

    async function handlePrintLabel(draft) {
        try {
            const token = await getToken()
            const url = `/api/shipping/drafts/${draft.id}/label?courier=${draft.courier}`
            const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
            if (res.status === 409) {
                // R5-B: label not ready yet (shipment not confirmed by courier) —
                // an informational, transient state, not an error.
                const body = await res.json().catch(() => ({}))
                pushToast({ kind: 'info', msg: body.message_pl || 'Etykieta nie jest jeszcze gotowa — spróbuj ponownie za chwilę.' })
                return
            }
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
            const blob = await res.blob()
            const objUrl = URL.createObjectURL(blob)
            window.open(objUrl, '_blank')
            setTimeout(() => URL.revokeObjectURL(objUrl), 30_000)
        } catch (e) {
            pushToast({ kind: 'error', msg: `Błąd pobierania etykiety: ${e.message}` })
        }
    }

    function handleExecute(draft, schedule) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}/execute`, {
                method: 'POST',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: schedule ? JSON.stringify(schedule) : null,
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się zrealizować przesyłki')()
    }

    function handlePickup(draft, schedule) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}/pickup`, {
                method: 'POST',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: schedule ? JSON.stringify(schedule) : null,
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się zamówić podjazdu')()
    }

    function handleSetApaczkaService(draft, serviceId) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ apaczka_service_id: serviceId, reviewed: true }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się zapisać usługi Apaczka')()
    }

    function handleReviewDraft(draft) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ reviewed: true }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się zatwierdzić draftu')()
    }

    function handleConfirmPending(draft) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}/confirm`, {
                method: 'POST',
                headers: { Authorization: `Bearer ${token}` },
            })
            // 202 = Allegro still processing, not an error — the auto-poll below
            // (or another manual click) will check again.
            if (!res.ok && res.status !== 202) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się sprawdzić statusu')()
    }

    // Auto-poll drafts stuck in pending_confirmation (Allegro create-command still
    // IN_PROGRESS) so the operator doesn't have to keep clicking "Sprawdź status".
    const pendingConfirmationKey = drafts
        .filter(d => d.status === 'pending_confirmation')
        .map(d => d.id)
        .join(',')

    useEffect(() => {
        if (!pendingConfirmationKey) return
        const ids = pendingConfirmationKey.split(',')
        const interval = setInterval(async () => {
            try {
                const token = await getToken()
                await Promise.all(ids.map(id =>
                    fetch(`/api/shipping/drafts/${id}/confirm`, {
                        method: 'POST',
                        headers: { Authorization: `Bearer ${token}` },
                    }).catch(() => {})
                ))
                load({ silent: true })
            } catch { /* retry on next tick */ }
        }, 5000)
        return () => clearInterval(interval)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [pendingConfirmationKey])

    function handleMarkFulfilled(draft) {
        const isAllegro = draft.source === 'allegro'
        const message = isAllegro
            ? 'Oznaczyć draft jako zrealizowany? Dodatkowo zmieni to status zamówienia w Allegro na PROCESSING — tej operacji nie da się cofnąć po stronie Allegro.'
            : 'Oznaczyć draft jako zrealizowany? Zmieni to tylko lokalny status w naszym systemie.'
        if (!window.confirm(message)) return
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}/mark-fulfilled`, {
                method: 'POST',
                headers: { Authorization: `Bearer ${token}` },
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
            await load()
        }, 'Nie udało się oznaczyć jako zrealizowane')()
    }


    function handleToggleSelect(id) {
        setSelectedDraftIds(prev => {
            const next = new Set(prev)
            if (next.has(id)) next.delete(id)
            else next.add(id)
            return next
        })
    }

    async function handleBulkExecute() {
        const ids = [...selectedDraftIds]
        setBulkProgress({ done: 0, total: ids.length })
        for (let i = 0; i < ids.length; i++) {
            const draft = drafts.find(d => d.id === ids[i])
            if (draft) {
                try { await handleExecute(draft) } catch { /* error visible in row */ }
            }
            setBulkProgress({ done: i + 1, total: ids.length })
        }
        setBulkProgress(null)
        setSelectedDraftIds(new Set())
        load()
    }


    async function handleBulkPickup(schedule) {
        setBulkPickupModal(false)
        const eligible = [...selectedDraftIds]
            .map(id => drafts.find(d => d.id === id))
            .filter(d => d && d.courier === 'inpost' && d.status === 'created' && !d.pickup_ordered)
        setBulkProgress({ done: 0, total: eligible.length })
        for (let i = 0; i < eligible.length; i++) {
            try { await handlePickup(eligible[i], schedule) } catch { /* error visible in row */ }
            setBulkProgress({ done: i + 1, total: eligible.length })
        }
        setBulkProgress(null)
        setSelectedDraftIds(new Set())
        load()
    }

    const filtered = drafts.filter(d => {
        if (filterStatus !== 'all' && d.status !== filterStatus) return false
        if (filterCourier !== 'all' && d.courier !== filterCourier) return false
        if (filterSource !== 'all' && (d.source || 'shopify') !== filterSource) return false
        if (filterDateFrom && (d.order_date || d.created_at)?.slice(0, 10) < filterDateFrom) return false
        if (search) {
            const q = search.toLowerCase()
            if (!d.shopify_order_number?.toLowerCase().includes(q) &&
                !d.customer_name?.toLowerCase().includes(q)) return false
        }
        return true
    })

    const visibleDrafts = useMemo(() => {
        return sortDrafts(
            filtered,
            sortState,
            (draft, columnId) => sortValue(draft, columnId, apaczkaServices)
        )
    }, [filtered, sortState, apaczkaServices])

    const selectableIds = visibleDrafts
        .filter(d => d.status === 'pending' || d.status === 'needs_review' || d.status === 'error' ||
            (d.courier === 'inpost' && d.status === 'created' && !d.pickup_ordered))
        .map(d => d.id)
    const allSelected = selectableIds.length > 0 && selectableIds.every(id => selectedDraftIds.has(id))
    function handleSelectAll() {
        if (allSelected) setSelectedDraftIds(new Set())
        else setSelectedDraftIds(new Set(selectableIds))
    }

    const errorCount = drafts.filter(d => d.status === 'error').length

    return (
        <>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead
                title={T.shipping_title ?? 'Wysyłki'}
                sub={T.shipping_sub ?? 'Drafty przesyłek tworzonych automatycznie przy złożeniu zamówienia Shopify'}
            />

            <div className="toolbar" style={{ flexWrap: 'wrap', gap: 8 }}>
                <div className="search">
                    <Icon name="search" size={14} />
                    <input
                        placeholder={T.sh_search ?? 'Szukaj po numerze lub kliencie…'}
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                    />
                    {search && (
                        <button className="btn-ghost" style={{ padding: '0 4px' }} onClick={() => setSearch('')}>
                            <Icon name="x" size={12} />
                        </button>
                    )}
                </div>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                    <select value={filterStatus} onChange={e => setFilterStatus(e.target.value)}
                        style={{ fontSize: '0.82em', padding: '4px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', cursor: 'pointer' }}>
                        <option value="all">{T.sh_filter_all_status ?? 'Wszystkie statusy'}</option>
                        <option value="pending">{T.sh_status_pending ?? 'oczekujące'}</option>
                        <option value="created">{T.sh_status_created ?? 'nadane'}</option>
                        <option value="pending_confirmation">{T.sh_status_pending_confirmation ?? 'czeka na Allegro'}</option>
                        <option value="error">{T.sh_status_error ?? 'błąd'}</option>
                    </select>
                    <select value={filterCourier} onChange={e => setFilterCourier(e.target.value)}
                        style={{ fontSize: '0.82em', padding: '4px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', cursor: 'pointer' }}>
                        <option value="all">{T.sh_filter_all_courier ?? 'Wszyscy kurierzy'}</option>
                        <option value="inpost">InPost</option>
                        <option value="apaczka">Apaczka</option>
                        <option value="allegro_delivery">Wysyłam z Allegro</option>
                    </select>
                    <select value={filterSource} onChange={e => setFilterSource(e.target.value)}
                        title={T.sh_filter_source ?? 'Źródło zamówienia'}
                        style={{ fontSize: '0.82em', padding: '4px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', cursor: 'pointer' }}>
                        <option value="all">{T.sh_filter_all_source ?? 'Wszystkie źródła'}</option>
                        <option value="shopify">Shopify</option>
                        <option value="allegro">Allegro</option>
                    </select>
                    <input type="date" value={filterDateFrom} onChange={e => setFilterDateFrom(e.target.value)}
                        title="From date"
                        style={{ fontSize: '0.82em', padding: '4px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: filterDateFrom ? 'var(--text)' : 'var(--text-3)', cursor: 'pointer' }}
                    />
                    {filterDateFrom && <button className="btn-ghost" style={{ padding: '0 4px', fontSize: '0.82em' }} onClick={() => setFilterDateFrom('')}><Icon name="x" size={12} /></button>}
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    {canManage && selectedDraftIds.size > 0 && (() => {
                        const pendingSelected = [...selectedDraftIds].filter(id => {
                            const d = drafts.find(x => x.id === id)
                            return d && (d.status === 'pending' || d.status === 'error')
                        })
                        const pickupSelected = [...selectedDraftIds].filter(id => {
                            const d = drafts.find(x => x.id === id)
                            return d && d.courier === 'inpost' && d.status === 'created' && !d.pickup_ordered
                        })
                        return (<>
                            {pendingSelected.length > 0 && (
                                <button
                                    className="btn btn-primary"
                                    style={{ fontSize: '0.85em' }}
                                    onClick={handleBulkExecute}
                                    disabled={bulkProgress !== null}
                                >
                                    {bulkProgress !== null
                                        ? `Realizuję ${bulkProgress.done}/${bulkProgress.total}…`
                                        : `Realizuj zaznaczone (${pendingSelected.length})`}
                                </button>
                            )}
                            {pickupSelected.length > 0 && (
                                <button
                                    className="btn btn-secondary"
                                    style={{ fontSize: '0.85em' }}
                                    onClick={() => setBulkPickupModal(true)}
                                    disabled={bulkProgress !== null}
                                >
                                    {bulkProgress !== null
                                        ? `Podjazd ${bulkProgress.done}/${bulkProgress.total}…`
                                        : `Zamów podjazd (${pickupSelected.length})`}
                                </button>
                            )}
                        </>)
                    })()}
                    <span className="mono dim">{drafts.length} {T.shipping_drafts_count ?? 'draftów'}</span>
                    {errorCount > 0 && (
                        <Pill kind="warn">{errorCount} {T.shipping_errors ?? 'błędów'}</Pill>
                    )}
                    <button className="btn btn-ghost" onClick={() => setExpandAll(v => !v)} style={{ fontSize: '0.82em', gap: 4 }} title={expandAll ? 'Collapse all' : 'Expand all'}>
                        <Icon name={expandAll ? 'chevronUp' : 'chevronDown'} size={13} />
                        {expandAll ? (T.sh_collapse ?? 'Zwiń') : (T.sh_expand ?? 'Rozwiń')}
                    </button>
                    <button className="btn btn-ghost" onClick={handleSync} disabled={syncing || loading} title="Synchronizuj zamówienia z Allegro i Shopify">
                        <Icon name={syncing ? 'refresh' : 'zap'} size={14} className={syncing ? 'spin' : undefined} />
                        {syncing ? 'Synchronizowanie...' : 'Synchronizuj'}
                        {syncResult?.error && <span style={{ color: 'var(--error)', fontSize: '0.75em', marginLeft: 4 }}>!</span>}
                    </button>
                    <button className="btn btn-ghost" onClick={load} disabled={loading} title="Odśwież widok">
                        <Icon name="refresh" size={14} className={loading ? 'spin' : undefined} />
                        {loading ? 'Odświeżanie...' : 'Odśwież'}
                    </button>
                </div>
            </div>

            <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
                {!loading && !error && filtered.length > 0 && (
                    <div style={{ display: 'flex', alignItems: 'center', minWidth: tableMinWidth, borderBottom: '2px solid var(--border-strong)', background: 'var(--surface-2)' }}>
                        <div style={{ width: 56, flexShrink: 0 }} />
                        <div className="shipping-table-header" style={{ gridTemplateColumns: columnGridTemplate }}>
                            {SHIPPING_COLUMNS.map(column => {
                                const active = sortState.key === column.id
                                const ariaSort = active
                                    ? (sortState.direction === 'asc' ? 'ascending' : 'descending')
                                    : 'none'
                                return (
                                    <div
                                        key={column.id}
                                        className="shipping-table-heading"
                                        role="columnheader"
                                        aria-sort={column.sortable ? ariaSort : undefined}
                                    >
                                        <button
                                            type="button"
                                            className="shipping-table-sort"
                                            onClick={() => handleSort(column)}
                                            disabled={!column.sortable}
                                            title={column.sortable ? `Sortuj: ${column.label}` : column.label}
                                        >
                                            <span>{column.label}</span>
                                            {active && (
                                                <Icon name={sortState.direction === 'asc' ? 'caretUp' : 'caret'} size={12} />
                                            )}
                                        </button>
                                        <button
                                            type="button"
                                            className="shipping-column-resize"
                                            onPointerDown={event => startColumnResize(event, column)}
                                            aria-label={`Zmień szerokość kolumny ${column.label}`}
                                            title={`Zmień szerokość kolumny ${column.label}`}
                                        />
                                    </div>
                                )
                            })}
                        </div>
                    </div>
                )}
                {!loading && !error && filtered.length > 0 && selectableIds.length > 0 && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: tableMinWidth, padding: '6px 16px 6px 0', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)' }}>
                        <div style={{ width: 56, flexShrink: 0, display: 'flex', justifyContent: 'center' }}>
                            <input type="checkbox" checked={allSelected} onChange={handleSelectAll}
                                style={{ cursor: 'pointer', accentColor: 'var(--primary, #3b82f6)' }} />
                        </div>
                        <span style={{ fontSize: '0.82em', color: 'var(--text-2)' }}>
                            {allSelected
                                ? `${T.sh_selected_all ?? 'Zaznaczono wszystkie'} (${selectableIds.length})`
                                : `${T.sh_select_all ?? 'Zaznacz wszystkie'} (${selectableIds.length})`}
                        </span>
                    </div>
                )}
                {loading && (
                    <div style={{ padding: 24, textAlign: 'center', color: 'var(--c-text-2)' }}>
                        Ładowanie…
                    </div>
                )}
                {error && (
                    <div className="error-banner" style={{ margin: 16 }}>
                        <Icon name="alertTriangle" size={14} />
                        {error}
                    </div>
                )}
                {!loading && !error && filtered.length === 0 && (
                    <div style={{ padding: 24, textAlign: 'center', color: 'var(--c-text-2)' }}>
                        {search ? 'Brak wyników.' : 'Brak draftów wysyłek.'}
                    </div>
                )}
                {!loading && visibleDrafts.map(draft => (
                    <DraftRow
                        key={draft.id}
                        draft={draft}
                        busy={busy}
                        canManage={canManage}
                        onPrintLabel={handlePrintLabel}
                        onExecute={handleExecute}
                        onPickup={handlePickup}
                        onMarkFulfilled={handleMarkFulfilled}
                        onConfirmPending={handleConfirmPending}
                        onSetApaczkaService={handleSetApaczkaService}
                        onReviewDraft={handleReviewDraft}
                        apaczkaServices={apaczkaServices}
                        selected={selectedDraftIds.has(draft.id)}
                        onToggleSelect={handleToggleSelect}
                        forceOpen={expandAll}
                        getToken={getToken}
                        onDraftUpdate={load}
                        columnGridTemplate={columnGridTemplate}
                        tableMinWidth={tableMinWidth}
                    />
                ))}
            </div>
        </div>
        {bulkPickupModal && (
            <PickupScheduleModal
                title="Zamów podjazd kuriera (wszystkie zaznaczone)"
                onConfirm={handleBulkPickup}
                onCancel={() => setBulkPickupModal(false)}
            />
        )}
        </>
    )
}

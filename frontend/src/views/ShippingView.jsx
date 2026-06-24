import { useState, useEffect, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Pill } from '../components/Pill'
import { Icon } from '../components/Icon'

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

function courierLabel(draft) {
    if (draft.courier === 'inpost') {
        if (draft.service === 'inpost_locker_standard') return 'InPost Paczkomat'
        if (draft.service === 'inpost_courier_standard') return 'InPost Kurier'
        return 'InPost'
    }
    return 'Apaczka'
}

function courierPillKind(draft) {
    if (draft.courier === 'inpost') return 'info'
    return 'default'
}

function sourcePillKind(source) {
    if (source === 'allegro') return 'warn'
    return 'default'
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

function PackagesInfo({ draft }) {
    const count = draft.packages_count ?? 1
    const items = draft.order_items ?? []
    const breakdown = draft.packages_breakdown ?? []
    const matTags = materialTags(items)
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <div>
                <span className="mono" style={{ fontWeight: 600 }}>{count}</span>
                <span className="dim"> {count === 1 ? 'paczka' : 'paczki'}</span>
            </div>
            {matTags.length > 0 && (
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                    {matTags.map(tag => (
                        <Chip key={tag.label} label={`${tag.label} ×${tag.count}`} style={tag} />
                    ))}
                </div>
            )}
            {breakdown.length > 0 && (
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                    {breakdown.map((b, i) => {
                        const s = _GLASS_TYPES.has(b.type) ? _BOX_STYLE.glass : _BOX_STYLE.plastic
                        return <Chip key={i} label={`${b.qty}×${b.type}`} style={s} />
                    })}
                </div>
            )}
        </div>
    )
}

const TIME_SLOTS = ['07:00','08:00','09:00','10:00','11:00','12:00','13:00','14:00','15:00','16:00','17:00','18:00']

function toMinutes(t) { const [h, m] = t.split(':').map(Number); return h * 60 + m }
function addHours(t, hrs) {
    const m = toMinutes(t) + hrs * 60
    return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
}

function BulkUpdateModal({ count, onConfirm, onCancel }) {
    const [value, setValue] = useState(1)
    const sel = { padding: '6px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.9em', width: '100%' }
    return createPortal(
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}
            onClick={e => { if (e.target === e.currentTarget) onCancel() }}>
            <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 24, minWidth: 280, display: 'flex', flexDirection: 'column', gap: 16 }}>
                <div style={{ fontWeight: 600 }}>Aktualizuj liczbę paczek ({count} draftów)</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>Liczba paczek</label>
                    <input type="number" min={1} max={99} value={value}
                        onChange={e => setValue(Math.max(1, parseInt(e.target.value) || 1))}
                        style={sel}
                        autoFocus
                    />
                </div>
                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                    <button className="btn btn-ghost" onClick={onCancel}>Anuluj</button>
                    <button className="btn btn-primary" onClick={() => onConfirm(value)}>Zastosuj</button>
                </div>
            </div>
        </div>,
        document.body
    )
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

function DraftRow({ draft, onPrintLabel, onExecute, onPickup, busy, canManage, selected, onToggleSelect, forceOpen }) {
    const { t, lang } = useT()
    const T = t[lang]
    const [open, setOpen] = useState(false)

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
        draft.status === 'error' ||
        (draft.courier === 'inpost' && draft.status === 'created' && !draft.pickup_ordered)
    )

    return (
        <div className={`accordion-row${open ? ' open' : ''}`} style={{ display: 'flex', alignItems: 'stretch' }}>
            <div style={{ width: 56, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6 }}>
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
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '6px 8px', color: 'var(--text-2)', display: 'flex', alignItems: 'center', borderRadius: 4 }}
                >
                    <Icon name={open ? 'chevronUp' : 'chevronDown'} size={20} />
                </button>
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
            <div
                className="accordion-header"
                style={{ padding: '10px 16px 10px 0', cursor: 'default', display: 'grid', alignItems: 'center',
                    gridTemplateColumns: '72px 1fr 170px 120px 130px 130px 88px 76px' }}
            >
                <span className="mono">#{draft.shopify_order_number}</span>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {draft.customer_name || '—'}
                    {draft.source && draft.source !== 'shopify' && (
                        <Pill kind={sourcePillKind(draft.source)} style={{ marginLeft: 4 }}>{draft.source}</Pill>
                    )}
                </span>
                <span className="dim" style={{ fontSize: '0.8em', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {draft.receiver?.email || ''}
                </span>
                <span className="dim mono" style={{ fontSize: '0.8em', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {draft.receiver?.phone || ''}
                </span>
                <span><Pill kind={courierPillKind(draft)}>{courierLabel(draft)}</Pill></span>
                <span className="mono dim" style={{ fontSize: '0.85em' }}>{fmtDate(draft.created_at)}</span>
                <span>
                    <Pill kind={draft.status === 'created' ? 'ok' : draft.status === 'pending' ? 'default' : 'warn'}>
                        {draft.status === 'pending' ? (T.sh_status_pending ?? 'oczekujące')
                            : draft.status === 'created' ? (T.sh_status_created ?? 'nadane')
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
                    <div className="detail-grid">
                        <div>
                            <>
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
                                            {draft.shipping_address?.street}<br />
                                            {draft.shipping_address?.post_code} {draft.shipping_address?.city}
                                        </div>
                                    )}
                            </>
                        </div>
                        <div>
                            <div className="detail-label">Email</div>
                            <div>{draft.receiver?.email || '—'}</div>
                        </div>
                        <div>
                            <div className="detail-label">Telefon</div>
                            <div className="mono">{draft.receiver?.phone || '—'}</div>
                        </div>
                        <div>
                            <div className="detail-label">Paczki</div>
                            <PackagesInfo draft={draft} />
                        </div>
                        <div>
                            <div className="detail-label">Numer śledzenia</div>
                            <div>
                                {draft.tracking_number
                                    ? (
                                        <span
                                            className="mono copyable"
                                            title="Kliknij żeby skopiować"
                                            onClick={() => navigator.clipboard.writeText(draft.tracking_number)}
                                            style={{ cursor: 'pointer' }}
                                        >
                                            {draft.tracking_number}
                                        </span>
                                    )
                                    : <span className="dim">—</span>}
                            </div>
                        </div>
                        <div>
                            <div className="detail-label">ID draftu kuriera</div>
                            <div className="mono dim">{draft.courier_draft_id || '—'}</div>
                        </div>
                    </div>

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

                        {canManage && canPickup && (
                            <button
                                className="btn btn-secondary"
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

    const [drafts, setDrafts] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)
    const [search, setSearch] = useState('')
    const [filterStatus, setFilterStatus] = useState('all')
    const [filterCourier, setFilterCourier] = useState('all')
    const [filterDateFrom, setFilterDateFrom] = useState('')
    const [busy, setBusy] = useState(new Set())
    const [selectedDraftIds, setSelectedDraftIds] = useState(new Set())
    const [bulkProgress, setBulkProgress] = useState(null)
    const [bulkPickupModal, setBulkPickupModal] = useState(false)
    const [bulkUpdateModal, setBulkUpdateModal] = useState(false)
    const [expandAll, setExpandAll] = useState(null)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const token = await getToken()
            const res = await fetch('/api/shipping/drafts', {
                headers: { Authorization: `Bearer ${token}` },
            })
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
            const data = await res.json()
            setDrafts(data.drafts ?? [])
        } catch (e) {
            setError(e.message)
        } finally {
            setLoading(false)
        }
    }, [getToken])

    useEffect(() => {
        let cancelled = false
        async function run() {
            setLoading(true)
            setError(null)
            try {
                const token = await getToken()
                const res = await fetch('/api/shipping/drafts', {
                    headers: { Authorization: `Bearer ${token}` },
                })
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
                const data = await res.json()
                if (!cancelled) setDrafts(data.drafts ?? [])
            } catch (e) {
                if (!cancelled) setError(e.message)
            } finally {
                if (!cancelled) setLoading(false)
            }
        }
        run()
        return () => { cancelled = true }
    }, [getToken])

    function withBusy(draftId, fn) {
        return async () => {
            setBusy(s => new Set([...s, draftId]))
            try {
                await fn()
                await load()
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
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
            const blob = await res.blob()
            const objUrl = URL.createObjectURL(blob)
            window.open(objUrl, '_blank')
            setTimeout(() => URL.revokeObjectURL(objUrl), 30_000)
        } catch (e) {
            alert(`Błąd pobierania etykiety: ${e.message}`)
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
                throw new Error(body.detail || `${res.status}`)
            }
        })()
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
                throw new Error(body.detail || `${res.status}`)
            }
        })()
    }

    function handleUpdateCount(draft, newCount) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ packages_count: newCount }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(body.detail || `${res.status}`)
            }
        })()
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

    async function handleBulkUpdate(newCount) {
        setBulkUpdateModal(false)
        const ids = [...selectedDraftIds]
        setBulkProgress({ done: 0, total: ids.length })
        for (let i = 0; i < ids.length; i++) {
            const draft = drafts.find(d => d.id === ids[i])
            if (draft) {
                try { await handleUpdateCount(draft, newCount) } catch { /* error stays in row */ }
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
        if (filterDateFrom && d.created_at?.slice(0, 10) < filterDateFrom) return false
        if (search) {
            const q = search.toLowerCase()
            if (!d.shopify_order_number?.toLowerCase().includes(q) &&
                !d.customer_name?.toLowerCase().includes(q)) return false
        }
        return true
    })

    const selectableIds = filtered
        .filter(d => d.status === 'pending' || d.status === 'error' ||
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
                        <option value="error">{T.sh_status_error ?? 'błąd'}</option>
                    </select>
                    <select value={filterCourier} onChange={e => setFilterCourier(e.target.value)}
                        style={{ fontSize: '0.82em', padding: '4px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', cursor: 'pointer' }}>
                        <option value="all">{T.sh_filter_all_courier ?? 'Wszyscy kurierzy'}</option>
                        <option value="inpost">InPost</option>
                        <option value="apaczka">Apaczka</option>
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
                            <button
                                className="btn btn-ghost"
                                style={{ fontSize: '0.85em' }}
                                onClick={() => setBulkUpdateModal(true)}
                                disabled={bulkProgress !== null}
                            >
                                {bulkProgress !== null
                                    ? `Aktualizuję ${bulkProgress.done}/${bulkProgress.total}…`
                                    : `Aktualizuj paczki (${selectedDraftIds.size})`}
                            </button>
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
                    <button className="btn btn-ghost" onClick={load} disabled={loading} title="Odśwież">
                        <Icon name="refreshCw" size={14} />
                    </button>
                </div>
            </div>

            <div className="card" style={{ padding: 0 }}>
                {!loading && !error && filtered.length > 0 && (
                    <div style={{ display: 'flex', alignItems: 'center', borderBottom: '2px solid var(--border-strong)', background: 'var(--surface-2)', fontSize: '11px', fontWeight: 600, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                        <div style={{ width: 56, flexShrink: 0 }} />
                        <div style={{ flex: 1, minWidth: 0, display: 'grid', alignItems: 'center', padding: '7px 16px 7px 0', gap: 12,
                            gridTemplateColumns: '72px 1fr 170px 120px 130px 130px 88px 76px' }}>
                            <span>Nr</span>
                            <span>Klient</span>
                            <span>Email</span>
                            <span>Telefon</span>
                            <span>Kurier</span>
                            <span>Data</span>
                            <span>Status</span>
                            <span>Podjazd</span>
                        </div>
                    </div>
                )}
                {!loading && !error && filtered.length > 0 && selectableIds.length > 0 && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 16px 6px 0', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)' }}>
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
                {!loading && filtered.map(draft => (
                    <DraftRow
                        key={draft.id}
                        draft={draft}
                        busy={busy}
                        canManage={canManage}
                        onPrintLabel={handlePrintLabel}
                        onExecute={handleExecute}
                        onPickup={handlePickup}
                        onUpdateCount={handleUpdateCount}
                        selected={selectedDraftIds.has(draft.id)}
                        onToggleSelect={handleToggleSelect}
                        forceOpen={expandAll}
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
        {bulkUpdateModal && (
            <BulkUpdateModal
                count={selectedDraftIds.size}
                onConfirm={handleBulkUpdate}
                onCancel={() => setBulkUpdateModal(false)}
            />
        )}
        </>
    )
}

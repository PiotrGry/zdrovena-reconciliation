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

function breakdownLabel(breakdown) {
    if (!breakdown || breakdown.length === 0) return null
    return breakdown.map(b => `${b.qty}×${b.type}`).join(' + ')
}

function PackagesInfo({ draft }) {
    const count = draft.packages_count ?? 1
    const items = draft.order_items ?? []
    const label = breakdownLabel(draft.packages_breakdown)
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <div>
                <span className="mono" style={{ fontWeight: 600 }}>{count}</span>
                <span className="dim"> {count === 1 ? 'paczka' : 'paczki'}</span>
            </div>
            {label && (
                <div className="dim" style={{ fontSize: '0.82em' }}>{label}</div>
            )}
            {items.length > 0 && (
                <div style={{ fontSize: '0.82em', marginTop: 2 }}>
                    {items.map((it, i) => (
                        <div key={i}>
                            <span className="mono">{it.quantity}×</span> {it.name}
                        </div>
                    ))}
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

function PickupScheduleModal({ onConfirm, onCancel, title }) {
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
                    <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>Pickup date</label>
                    <input type="date" value={date} min={today}
                        onChange={e => { handleDateChange(e.target.value); e.target.blur() }}
                        style={sel}
                    />
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>From</label>
                        <select value={from} onChange={e => handleFromChange(e.target.value)} style={sel}>
                            {TIME_SLOTS.filter(t => t >= minFrom && t <= '16:00').map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                    </div>
                    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>To</label>
                        <select value={to} onChange={e => setTo(e.target.value)} style={sel}>
                            {TIME_SLOTS.filter(t => toMinutes(t) >= toMinutes(from) + 120).map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                    </div>
                </div>
                <div style={{ fontSize: '0.8em', color: 'var(--text-2)' }}>Minimum window: 2 hours</div>
                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                    <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
                    <button className="btn btn-primary"
                        onClick={() => onConfirm({ pickup_date: date, pickup_from: from, pickup_to: to })}>
                        Confirm
                    </button>
                </div>
            </div>
        </div>,
        document.body
    )
}

function DraftRow({ draft, onPrintLabel, onExecute, onPickup, busy, canManage, selected, onToggleSelect }) {
    const [open, setOpen] = useState(false)
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
            <div style={{ width: 40, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                {isSelectable ? (
                    <input
                        type="checkbox"
                        checked={selected || false}
                        onChange={() => onToggleSelect(draft.id)}
                        style={{ cursor: 'pointer', accentColor: 'var(--primary, #3b82f6)' }}
                    />
                ) : <span style={{ width: 16 }} />}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
            <button
                className="accordion-header"
                onClick={() => setOpen(o => !o)}
                aria-expanded={open}
                style={{ paddingLeft: 0 }}
            >
                <span className="mono" style={{ minWidth: 80 }}>#{draft.shopify_order_number}</span>
                <span style={{ flex: 1, textAlign: 'left' }}>{draft.customer_name || '—'}</span>
                {draft.source && draft.source !== 'shopify' && (
                    <Pill kind={sourcePillKind(draft.source)}>{draft.source}</Pill>
                )}
                <Pill kind={courierPillKind(draft)}>{courierLabel(draft)}</Pill>
                <span className="mono dim" style={{ minWidth: 130, textAlign: 'right' }}>
                    {fmtDate(draft.created_at)}
                </span>
                <Pill kind={draft.status === 'created' ? 'ok' : draft.status === 'pending' ? 'default' : 'warn'}>
                    {draft.status}
                </Pill>
                {draft.pickup_ordered && (
                    <span style={{ fontSize: '0.72em', padding: '2px 7px', borderRadius: 4, background: 'var(--ok-subtle, #f0fdf4)', color: 'var(--ok, #16a34a)', border: '1px solid var(--ok-border, #86efac)', whiteSpace: 'nowrap' }}>
                        pickup ✓
                    </span>
                )}
                <Icon name={open ? 'chevronUp' : 'chevronDown'} size={14} className="icon" />
            </button>

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
                        placeholder="Search by order # or customer…"
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
                        <option value="all">All statuses</option>
                        <option value="pending">Pending</option>
                        <option value="created">Created</option>
                        <option value="error">Error</option>
                    </select>
                    <select value={filterCourier} onChange={e => setFilterCourier(e.target.value)}
                        style={{ fontSize: '0.82em', padding: '4px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', cursor: 'pointer' }}>
                        <option value="all">All couriers</option>
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
                        </>)
                    })()}
                    <span className="mono dim">{drafts.length} {T.shipping_drafts_count ?? 'draftów'}</span>
                    {errorCount > 0 && (
                        <Pill kind="warn">{errorCount} {T.shipping_errors ?? 'błędów'}</Pill>
                    )}
                    <button className="btn btn-ghost" onClick={load} disabled={loading} title="Odśwież">
                        <Icon name="refreshCw" size={14} />
                    </button>
                </div>
            </div>

            <div className="card" style={{ padding: 0 }}>
                {!loading && !error && filtered.length > 0 && selectableIds.length > 0 && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 16px 6px 0', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)' }}>
                        <div style={{ width: 40, display: 'flex', justifyContent: 'center' }}>
                            <input type="checkbox" checked={allSelected} onChange={handleSelectAll}
                                style={{ cursor: 'pointer', accentColor: 'var(--primary, #3b82f6)' }} />
                        </div>
                        <span style={{ fontSize: '0.82em', color: 'var(--text-2)' }}>
                            {allSelected ? `All ${selectableIds.length} selected` : `Select all ${selectableIds.length} actionable`}
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

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

function packagesBreakdown(totalQty) {
    const qty = totalQty || 1
    const b3 = Math.floor(qty / 3)
    const rest = qty % 3
    const parts = []
    if (b3 > 0) parts.push(`${b3}×3-pak`)
    if (rest === 2) parts.push(`1×2-pak`)
    if (rest === 1) parts.push(`1×1-pak`)
    return parts.join(' + ')
}

function PackagesInfo({ draft }) {
    const qty = draft.total_qty ?? 1
    const count = draft.packages_count ?? 1
    const items = draft.order_items ?? []
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <div>
                <span className="mono" style={{ fontWeight: 600 }}>{count}</span>
                <span className="dim"> {count === 1 ? 'paczka' : 'paczki'}</span>
            </div>
            <div className="dim" style={{ fontSize: '0.82em' }}>
                {packagesBreakdown(qty)}
            </div>
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

function InPostServiceToggle({ draft, onUpdateService }) {
    const isPaczkomat = draft.service === 'inpost_locker_standard'
    const [lockerId, setLockerId] = useState(draft.receiver?.locker_id || '')
    const [lockerDirty, setLockerDirty] = useState(false)

    function handleServiceChange(newService) {
        onUpdateService(draft, { service: newService })
    }

    function handleLockerSave() {
        onUpdateService(draft, { locker_id: lockerId })
        setLockerDirty(false)
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div className="detail-label">Metoda dostawy InPost</div>
            <div style={{ display: 'flex', gap: 4 }}>
                <button
                    className={`btn btn-sm ${isPaczkomat ? 'btn-primary' : 'btn-ghost'}`}
                    onClick={() => !isPaczkomat && handleServiceChange('inpost_locker_standard')}
                    style={{ fontSize: '0.82em' }}
                >
                    <Icon name="package" size={12} /> Paczkomat
                </button>
                <button
                    className={`btn btn-sm ${!isPaczkomat ? 'btn-primary' : 'btn-ghost'}`}
                    onClick={() => isPaczkomat && handleServiceChange('inpost_courier_standard')}
                    style={{ fontSize: '0.82em' }}
                >
                    <Icon name="truck" size={12} /> Kurier
                </button>
            </div>
            {isPaczkomat ? (
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                    <input
                        className="mono"
                        placeholder="ID paczkomatu (np. WAW123A)"
                        value={lockerId}
                        onChange={e => { setLockerId(e.target.value.toUpperCase()); setLockerDirty(true) }}
                        style={{ padding: '4px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.85em', width: 180 }}
                    />
                    {lockerDirty && (
                        <button className="btn btn-sm btn-secondary" onClick={handleLockerSave} style={{ fontSize: '0.82em' }}>
                            Zapisz
                        </button>
                    )}
                    {draft.shipping_address?.city && (
                        <span className="dim" style={{ fontSize: '0.85em' }}>{draft.shipping_address.city}</span>
                    )}
                </div>
            ) : (
                <div style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>
                    {draft.shipping_address?.street}<br />
                    {draft.shipping_address?.post_code} {draft.shipping_address?.city}
                </div>
            )}
        </div>
    )
}

function PickupScheduleModal({ onConfirm, onCancel, title }) {
    const today = new Date().toISOString().slice(0, 10)
    const [date, setDate] = useState(today)
    const [from, setFrom] = useState('09:00')
    const [to, setTo] = useState('14:00')

    return createPortal(
        <div style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }}>
            <div style={{
                background: 'var(--bg)', border: '1px solid var(--border)',
                borderRadius: 8, padding: 24, minWidth: 300, display: 'flex', flexDirection: 'column', gap: 16,
            }}>
                <div style={{ fontWeight: 600 }}>{title}</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>Data podjazdu</label>
                    <input type="date" value={date} min={today}
                        onChange={e => setDate(e.target.value)}
                        style={{ padding: '6px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.9em' }}
                    />
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>Od</label>
                        <input type="time" value={from} onChange={e => setFrom(e.target.value)}
                            style={{ padding: '6px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.9em' }}
                        />
                    </div>
                    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>Do</label>
                        <input type="time" value={to} onChange={e => setTo(e.target.value)}
                            style={{ padding: '6px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.9em' }}
                        />
                    </div>
                </div>
                <div style={{ fontSize: '0.8em', color: 'var(--text-2)' }}>Minimalne okno: 2 godziny</div>
                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                    <button className="btn btn-ghost" onClick={onCancel}>Anuluj</button>
                    <button className="btn btn-primary"
                        onClick={() => onConfirm({ pickup_date: date, pickup_from: from, pickup_to: to })}>
                        Potwierdź
                    </button>
                </div>
            </div>
        </div>,
        document.body
    )
}

function DraftRow({ draft, onPrintLabel, onExecute, onPickup, onUpdateCount, onUpdateService, busy, canManage }) {
    const [open, setOpen] = useState(false)
    const [pickupModal, setPickupModal] = useState(null) // 'execute' | 'pickup' | null
    const isBusy = busy.has(draft.id)
    const needsPickupSchedule = draft.courier === 'inpost'
    const canPickup = (
        needsPickupSchedule &&
        draft.status === 'created' &&
        !draft.pickup_ordered
    )

    return (
        <div className={`accordion-row${open ? ' open' : ''}`}>
            <button
                className="accordion-header"
                onClick={() => setOpen(o => !o)}
                aria-expanded={open}
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
                <Icon name={open ? 'chevronUp' : 'chevronDown'} size={14} className="icon" />
            </button>

            {open && (
                <div className="accordion-body">
                    <div className="detail-grid">
                        <div>
                            {canManage && draft.courier === 'inpost' && (draft.status === 'pending' || draft.status === 'error') ? (
                                <InPostServiceToggle draft={draft} onUpdateService={onUpdateService} />
                            ) : (
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
                            )}
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
    const [busy, setBusy] = useState(new Set())

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

    function handleUpdateService(draft, fields) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify(fields),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(body.detail || `${res.status}`)
            }
        })()
    }

    const filtered = drafts.filter(d => {
        if (!search) return true
        const q = search.toLowerCase()
        return (
            d.shopify_order_number?.toLowerCase().includes(q) ||
            d.customer_name?.toLowerCase().includes(q) ||
            d.courier?.toLowerCase().includes(q)
        )
    })

    const errorCount = drafts.filter(d => d.status === 'error').length

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead
                title={T.shipping_title ?? 'Wysyłki'}
                sub={T.shipping_sub ?? 'Drafty przesyłek tworzonych automatycznie przy złożeniu zamówienia Shopify'}
            />

            <div className="toolbar">
                <div className="search">
                    <Icon name="search" size={14} />
                    <input
                        placeholder="Szukaj po numerze zamówienia lub kliencie…"
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                    />
                    {search && (
                        <button className="btn-ghost" style={{ padding: '0 4px' }} onClick={() => setSearch('')}>
                            <Icon name="x" size={12} />
                        </button>
                    )}
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
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
                        onUpdateService={handleUpdateService}
                    />
                ))}
            </div>
        </div>
    )
}

import { useState, useEffect } from 'react'
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

function DraftRow({ draft, onPrintLabel }) {
    const [open, setOpen] = useState(false)

    return (
        <div className={`accordion-row${open ? ' open' : ''}`}>
            <button
                className="accordion-header"
                onClick={() => setOpen(o => !o)}
                aria-expanded={open}
            >
                <span className="mono" style={{ minWidth: 80 }}>#{draft.shopify_order_number}</span>
                <span style={{ flex: 1, textAlign: 'left' }}>{draft.customer_name || '—'}</span>
                <Pill kind={courierPillKind(draft)}>{courierLabel(draft)}</Pill>
                <span className="mono dim" style={{ minWidth: 130, textAlign: 'right' }}>
                    {fmtDate(draft.created_at)}
                </span>
                <Pill kind={draft.status === 'created' ? 'ok' : 'warn'}>
                    {draft.status === 'created' ? 'created' : 'error'}
                </Pill>
                <Icon name={open ? 'chevronUp' : 'chevronDown'} size={14} className="icon" />
            </button>

            {open && (
                <div className="accordion-body">
                    <div className="detail-grid">
                        <div>
                            <div className="detail-label">Adres dostawy</div>
                            <div>
                                {draft.shipping_address?.street}<br />
                                {draft.shipping_address?.post_code} {draft.shipping_address?.city}
                            </div>
                        </div>
                        <div>
                            <div className="detail-label">Paczka</div>
                            <div>
                                {draft.parcel?.template
                                    ? `Szablon: ${draft.parcel.template}`
                                    : draft.parcel?.weight_kg
                                        ? `Waga: ${draft.parcel.weight_kg} kg`
                                        : '—'}
                            </div>
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

                    {draft.courier_draft_id && draft.status === 'created' && (
                        <div style={{ marginTop: 12 }}>
                            <button
                                className="btn btn-secondary"
                                onClick={() => onPrintLabel(draft)}
                            >
                                <Icon name="printer" size={14} />
                                Drukuj etykietę
                            </button>
                        </div>
                    )}
                </div>
            )}
        </div>
    )
}

export default function ShippingView() {
    const { getToken } = useAuth()
    const { t, lang } = useT()
    const T = t[lang]

    const [drafts, setDrafts] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)
    const [search, setSearch] = useState('')

    useEffect(() => {
        let cancelled = false
        async function load() {
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
        load()
        return () => { cancelled = true }
    }, [getToken])

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
                        onPrintLabel={handlePrintLabel}
                    />
                ))}
            </div>
        </div>
    )
}

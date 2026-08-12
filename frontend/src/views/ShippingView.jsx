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

export function printPdf(blob, title) {
    const url = URL.createObjectURL(blob)
    const frame = document.createElement('iframe')
    frame.title = title
    // Safari can print a blank PDF when its iframe is visibility:hidden. Keep
    // it renderable while placing it outside the visible viewport.
    frame.style.cssText = 'position:fixed;left:-10000px;top:-10000px;width:1px;height:1px;border:0'
    frame.src = url
    frame.onload = () => {
        frame.contentWindow?.focus()
        frame.contentWindow?.print()
    }
    document.body.appendChild(frame)
    window.setTimeout(() => {
        URL.revokeObjectURL(url)
        frame.remove()
    }, 60_000)
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

function matchStatusLabel(status) {
    switch (status) {
        case 'auto_matched':
            return 'Dopasowano automatycznie'
        case 'manual':
            return 'Wybrano ręcznie'
        case 'unrecognized':
            return 'Nie rozpoznano'
        case 'requires_selection':
            return 'Wymaga wyboru'
        default:
            return 'Wymaga wyboru'
    }
}

function matchStatusPillKind(status) {
    if (status === 'auto_matched') return 'ok'
    if (status === 'manual') return 'info'
    return 'warn'
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
        const ctrl = new AbortController()
        const timer = window.setTimeout(() => {
            if (ctrl.signal.aborted) return

            // R4.3/#135: a fresh preview load (draft change OR reload) must clear
            // prior mismatch acknowledgement — consent must never carry across
            // drafts or across preview versions of the same draft.
            setAckMismatch(false)
            setLoading(true)
            setError(null)
            void getToken().then(token =>
                fetchJson(`/api/shipping/drafts/${draft.id}/invoice-preview`, {
                    token,
                    signal: ctrl.signal,
                })
            ).then(data => {
                if (!ctrl.signal.aborted) { setPreview(data); setLoading(false) }
            }).catch(e => {
                if (e.name !== 'AbortError' && !ctrl.signal.aborted) {
                    setError(e.message)
                    setLoading(false)
                }
            })
        }, 0)
        return () => {
            window.clearTimeout(timer)
            ctrl.abort()
        }
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
                    {preview?.status === 'retry_ready' && (
                        <div className="error-banner">
                            <Icon name="alertTriangle" size={13} /> Automatyczne wystawienie faktury nie zostało dokończone: {preview.error}
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
                                <div style={{
                                    marginTop: 12, padding: '8px 12px', borderRadius: 6, fontSize: '0.88em',
                                    background: preview.matches_allegro ? 'var(--ok-bg, #f0fdf4)' : 'var(--warn-bg, #fffbeb)',
                                    border: `1px solid ${preview.matches_allegro ? 'var(--ok, #86efac)' : 'var(--warn, #fcd34d)'}`
                                }}>
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
                    {(preview?.status === 'preview_ready' || preview?.status === 'retry_ready') && (
                        <button className="btn btn-primary" onClick={handleCreate} disabled={creating || (preview.matches_allegro === false && !ackMismatch)}>
                            {creating
                                ? <><Icon name="loader" size={13} className="spin" /> Tworzenie…</>
                                : <><Icon name="invoice" size={13} /> {preview.status === 'retry_ready' ? 'Ponów automatyzację' : 'Utwórz i załącz do Allegro'}</>
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
    glass: { color: '#7c3aed', bg: '#f5f3ff', border: '#ddd6fe' },
}

const _PACKAGE_UNITS = {
    '3-pak': { material: 'plastik', amount: 3 },
    '2-pak': { material: 'plastik', amount: 2 },
    '1-pak': { material: 'plastik', amount: 1 },
    'pół-pak': { material: 'plastik', amount: 0.5 },
    'szkło-2pak': { material: 'szkło', amount: 2 },
    'szkło': { material: 'szkło', amount: 1 },
}

function materialTags(breakdown) {
    let plastic = 0, glass = 0
    for (const box of breakdown) {
        const packageInfo = _PACKAGE_UNITS[box.type]
        if (!packageInfo) continue
        const amount = packageInfo.amount * (box.qty ?? 1)
        if (packageInfo.material === 'szkło') glass += amount
        else plastic += amount
    }
    const tags = []
    if (plastic > 0) tags.push({ label: `plastik: ${String(plastic).replace('.', ',')} zgrzewki`, ..._BOX_STYLE.plastic })
    if (glass > 0) tags.push({ label: `szkło: ${String(glass).replace('.', ',')} zgrzewki`, ..._BOX_STYLE.glass })
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
    const tags = materialTags(draft.packages_breakdown ?? [])
    return (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {tags.map(tag => (
                <Chip key={tag.label} label={tag.label} style={tag} />
            ))}
        </div>
    )
}

const TIME_SLOTS = ['07:00', '08:00', '09:00', '10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '16:00', '17:00', '18:00']
const APACZKA_PICKUP_WINDOWS = [
    ['09:00', '17:00'],
    ['11:00', '14:00'],
    ['14:00', '17:00'],
]

function hasFixedApaczkaPickupWindows(draft) {
    return draft.courier === 'apaczka' && draft.apaczka_service_id === '23'
}

function toMinutes(t) { const [h, m] = t.split(':').map(Number); return h * 60 + m }
function addHours(t, hrs) {
    const m = toMinutes(t) + hrs * 60
    return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
}

function defaultPickupSchedule(fixedWindows = false) {
    const now = new Date()
    const today = now.toISOString().slice(0, 10)
    const minFromToday = addHours(`${String(now.getHours()).padStart(2, '0')}:00`, 2)
    if (fixedWindows) {
        const [from, to] = APACZKA_PICKUP_WINDOWS.find(([start]) => start >= minFromToday)
            || APACZKA_PICKUP_WINDOWS[0]
        return { pickup_date: today, pickup_from: from, pickup_to: to }
    }
    const from = TIME_SLOTS.find(t => t >= minFromToday && t <= '16:00') || '09:00'
    return { pickup_date: today, pickup_from: from, pickup_to: addHours(from, 2) }
}


function previewLine(label, value) {
    if (!value) return null
    return (
        <div style={{ display: 'flex', gap: 8, fontSize: '0.85em' }}>
            <span style={{ color: 'var(--text-2)', minWidth: 96 }}>{label}</span>
            <span style={{ fontWeight: 500 }}>{value}</span>
        </div>
    )
}

function formatAddress(addr) {
    if (!addr) return ''
    const line = [addr.street, addr.building_number].filter(Boolean).join(' ')
    const city = [addr.post_code, addr.city].filter(Boolean).join(' ')
    return [line, city].filter(Boolean).join(', ')
}

/** Render a single ShipX parcel the way the courier will read it, not the way we stored it. */
/**
 * Apaczka's order shape is nothing like ShipX's, so read it on its own terms
 * rather than forcing one into the other. Reformatting either into a shared
 * intermediate would risk showing the operator something the courier never sees.
 */
function ApaczkaPreviewParcel({ entry }) {
    const payload = entry.payload || {}
    const receiver = payload.address?.receiver || {}
    const box = (payload.shipment || [])[0] || {}
    const dimsText = box.dimension1
        ? `${box.dimension1} × ${box.dimension2} × ${box.dimension3} cm`
        : ''

    return (
        <div style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {previewLine('Usługa', payload.service_id ? `Apaczka #${payload.service_id}` : entry.service)}
            {previewLine('Referencja', payload.externalId || entry.reference)}
            {previewLine('Odbiorca', receiver.contact_person || receiver.name)}
            {previewLine('Adres', [receiver.line1, [receiver.postal_code, receiver.city].filter(Boolean).join(' ')].filter(Boolean).join(', '))}
            {previewLine('Telefon', receiver.phone)}
            {previewLine('Punkt odbioru', receiver.foreign_address_id)}
            {previewLine('Wymiary', dimsText)}
            {previewLine('Waga', box.weight != null ? `${box.weight} kg` : '')}
        </div>
    )
}

function AllegroPreviewParcel({ entry }) {
    const payload = entry.payload || {}
    const receiver = payload.receiver || {}
    const box = (payload.packages || [])[0] || {}
    const dimsText = box.length
        ? `${box.length.value} × ${box.width?.value} × ${box.height?.value} cm`
        : ''

    return (
        <div style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {previewLine('Usługa', entry.service)}
            {previewLine('Zamówienie Allegro', payload.order_id || entry.reference)}
            {previewLine('Odbiorca', receiver.name)}
            {previewLine('Adres', [receiver.street, [receiver.postCode, receiver.city].filter(Boolean).join(' ')].filter(Boolean).join(', '))}
            {previewLine('Punkt odbioru', receiver.point)}
            {previewLine('Wymiary', dimsText)}
            {previewLine('Waga', box.weight?.value != null ? `${box.weight.value} kg` : '')}
        </div>
    )
}

function ExecutePreviewParcel({ entry }) {
    const payload = entry.payload || {}
    // Three couriers, three unrelated payload shapes. Read each on its own
    // terms: a shared intermediate could show something no courier receives.
    if (payload.address) return <ApaczkaPreviewParcel entry={entry} />
    if (payload.packages) return <AllegroPreviewParcel entry={entry} />
    const parcel = (payload.parcels || [])[0] || {}
    const dims = parcel.dimensions
    // ShipX carries dimensions in mm; the operator thinks in cm, as the boxes are labelled.
    const dimsText = dims
        ? `${dims.length / 10} × ${dims.width / 10} × ${dims.height / 10} cm`
        : (parcel.template ? `szablon paczkomatu: ${parcel.template}` : '')
    const weight = parcel.weight?.amount
    const receiver = payload.receiver || {}
    const target = payload.custom_attributes?.target_point

    return (
        <div style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {previewLine('Usługa', payload.service || entry.service)}
            {previewLine('Referencja', payload.reference || entry.reference)}
            {previewLine('Odbiorca', [receiver.first_name, receiver.last_name].filter(Boolean).join(' '))}
            {previewLine('Adres', target ? `Paczkomat ${target}` : formatAddress(receiver.address))}
            {previewLine('Telefon', receiver.phone)}
            {previewLine('Wymiary', dimsText)}
            {previewLine('Waga', weight != null ? `${weight} kg` : '')}
        </div>
    )
}

/**
 * The payload the courier is about to receive, shown before anything is sent.
 *
 * Read straight from the preview endpoint rather than re-derived from the draft:
 * a panel that reconstructed the payload itself could show something the courier
 * never sees, which is worse than showing nothing.
 */
function ExecutePreview({ state }) {
    if (state.loading) return <div style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>Wczytywanie podglądu…</div>
    if (state.error) {
        return (
            <div className="error-banner">
                <Icon name="alertTriangle" size={13} />
                Nie udało się pobrać podglądu: {state.error}
            </div>
        )
    }
    const data = state.data || {}
    const parcels = data.parcels || []
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, maxWidth: 480 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {previewLine('Nadawca', data.sender?.name)}
                {previewLine('Adres nadania', formatAddress(data.sender))}
                {previewLine('Kurier', data.courier)}
            </div>
            {data.note && (
                <div style={{ fontSize: '0.8em', color: 'var(--text-2)' }}>{data.note}</div>
            )}
            {parcels.map(entry => (
                <ExecutePreviewParcel key={`${entry.package_type}-${entry.package_number}`} entry={entry} />
            ))}
        </div>
    )
}

function PickupScheduleModal({
    onConfirm,
    onCancel,
    title,
    children,
    summary,
    withSchedule = true,
    panelTestId,
    confirmTestId,
    confirmLabel,
    confirmDisabled = false,
    fixedWindows = false,
    initialSchedule,
    onScheduleChange,
}) {
    const { t, lang } = useT()
    const T = t[lang]
    const now = new Date()
    const today = now.toISOString().slice(0, 10)
    // Earliest allowed "from" on today: current hour + 2, rounded up to next slot
    const minFromToday = addHours(
        `${String(now.getHours()).padStart(2, '0')}:00`,
        2
    )

    const initial = initialSchedule || defaultPickupSchedule(fixedWindows)
    const [date, setDate] = useState(initial.pickup_date)
    const [from, setFrom] = useState(initial.pickup_from)
    const [to, setTo] = useState(initial.pickup_to)

    const isToday = date === today
    const minFrom = isToday ? minFromToday : '07:00'

    function handleFromChange(val) {
        setFrom(val)
        const nextTo = toMinutes(to) < toMinutes(val) + 120 ? addHours(val, 2) : to
        setTo(nextTo)
        onScheduleChange?.({ pickup_date: date, pickup_from: val, pickup_to: nextTo })
    }

    function handleDateChange(val) {
        setDate(val)
        if (fixedWindows) {
            const [nextFrom, nextTo] = val === today
                ? (APACZKA_PICKUP_WINDOWS.find(([start]) => start >= minFromToday)
                    || APACZKA_PICKUP_WINDOWS[0])
                : [from, to]
            setFrom(nextFrom)
            setTo(nextTo)
            onScheduleChange?.({ pickup_date: val, pickup_from: nextFrom, pickup_to: nextTo })
            return
        }
        // When switching to today, ensure from is still valid
        if (val === today && from < minFromToday) {
            const first = TIME_SLOTS.find(t => t >= minFromToday && t <= '16:00') || '09:00'
            setFrom(first)
            setTo(addHours(first, 2))
            onScheduleChange?.({ pickup_date: val, pickup_from: first, pickup_to: addHours(first, 2) })
        } else {
            onScheduleChange?.({ pickup_date: val, pickup_from: from, pickup_to: to })
        }
    }

    function handleFixedWindowChange(value) {
        const [nextFrom, nextTo] = value.split('|')
        setFrom(nextFrom)
        setTo(nextTo)
        onScheduleChange?.({ pickup_date: date, pickup_from: nextFrom, pickup_to: nextTo })
    }

    const sel = { padding: '6px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.9em', cursor: 'pointer' }

    return createPortal(
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}
            onClick={e => { if (e.target === e.currentTarget) onCancel() }}>
            <div
                data-testid={panelTestId}
                style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 24, minWidth: 320, maxHeight: '85vh', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 16 }}
            >
                <div style={{ fontWeight: 600 }}>{title}</div>
                {children}
                {summary}
                {withSchedule && (
                    <>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                            <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>{T.sh_pickup_date ?? 'Data podjazdu'}</label>
                            <input type="date" value={date} min={today}
                                onChange={e => { handleDateChange(e.target.value); e.target.blur() }}
                                style={sel}
                            />
                        </div>
                        {fixedWindows ? (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>Okno Apaczka</label>
                                <select
                                    data-testid="apaczka-pickup-window"
                                    value={`${from}|${to}`}
                                    onChange={e => handleFixedWindowChange(e.target.value)}
                                    style={sel}
                                >
                                    {APACZKA_PICKUP_WINDOWS.map(([start, end]) => (
                                        <option key={`${start}|${end}`} value={`${start}|${end}`}>
                                            {start}–{end}
                                        </option>
                                    ))}
                                </select>
                            </div>
                        ) : <div style={{ display: 'flex', gap: 8 }}>
                            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                                <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>{T.sh_time_from ?? 'Od'}</label>
                                <select value={from} onChange={e => handleFromChange(e.target.value)} style={sel}>
                                    {TIME_SLOTS.filter(t => t >= minFrom && t <= '16:00').map(t => <option key={t} value={t}>{t}</option>)}
                                </select>
                            </div>
                            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                                <label style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>{T.sh_time_to ?? 'Do'}</label>
                                <select value={to} onChange={e => {
                                    setTo(e.target.value)
                                    onScheduleChange?.({ pickup_date: date, pickup_from: from, pickup_to: e.target.value })
                                }} style={sel}>
                                    {TIME_SLOTS.filter(t => toMinutes(t) >= toMinutes(from) + 120).map(t => <option key={t} value={t}>{t}</option>)}
                                </select>
                            </div>
                        </div>}
                        {!fixedWindows && <div style={{ fontSize: '0.8em', color: 'var(--text-2)' }}>{T.sh_min_window ?? 'Minimalne okno: 2 godziny'}</div>}
                        {/* Say the window back in full. The date input renders per
                            locale and the hours live in two separate selects, so
                            without this the operator never sees the exact value
                            that will be sent — which for Apaczka cannot be undone
                            without cancelling the shipment. */}
                        <div data-testid="pickup-window-summary" style={{ fontSize: '0.85em' }}>
                            {T.sh_pickup_window ?? 'Podjazd'}: <strong>{date}</strong>, <strong>{from}–{to}</strong>
                        </div>
                    </>
                )}
                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                    <button className="btn btn-ghost" onClick={onCancel}>{T.sh_cancel ?? 'Anuluj'}</button>
                    <button className="btn btn-primary"
                        data-testid={confirmTestId}
                        disabled={confirmDisabled}
                        onClick={() => onConfirm(
                            withSchedule ? { pickup_date: date, pickup_from: from, pickup_to: to } : null
                        )}>
                        {confirmLabel ?? T.sh_confirm ?? 'Potwierdź'}
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
    const [open, setOpen] = useState(forceOpen ?? false)
    const apaczkaServiceBaseline = draft.apaczka_service_id || ''
    const [apaczkaServiceEdit, setApaczkaServiceEdit] = useState(null)
    const selectedApaczkaService = (
        apaczkaServiceEdit?.baseline === apaczkaServiceBaseline
            ? apaczkaServiceEdit.value
            : apaczkaServiceBaseline
    )
    const matchStatusBaseline = draft.shipping_service_match_status || ''
    const [editingOverride, setEditingOverride] = useState(null)
    const editingApaczkaService = (
        editingOverride?.baseline === matchStatusBaseline
            ? editingOverride.value
            : matchStatusBaseline !== 'auto_matched'
    )
    const [showInvoicePanel, setShowInvoicePanel] = useState(false)
    const invoiceIdBaseline = draft.fakturownia_invoice_id || null
    const [invoiceIdOverride, setInvoiceIdOverride] = useState(null)
    const localInvoiceId = (
        invoiceIdOverride?.baseline === invoiceIdBaseline
            ? invoiceIdOverride.value
            : invoiceIdBaseline
    )
    const [pickupModal, setPickupModal] = useState(null) // 'pickup' | null
    // { loading } | { error } | { data } — null while no preview is open.
    const [executePreview, setExecutePreview] = useState(null)
    const [executeSchedule, setExecuteSchedule] = useState(null)
    const executePreviewRequest = useRef(0)
    const isBusy = busy.has(draft.id)
    const matchedApaczkaService = apaczkaServices.find(
        service => service.service_id === draft.apaczka_service_id
    )
    const showApaczkaServiceEditor = (
        editingApaczkaService ||
        draft.shipping_service_match_status !== 'auto_matched' ||
        !matchedApaczkaService
    )
    // Apaczka is absent on purpose: its API has no standalone pickup call, so a
    // pickup can only travel inside order_send at execute time.
    const canOrderPickup = draft.courier === 'inpost' || draft.courier === 'allegro_delivery'
    const canPickup = (
        canOrderPickup &&
        draft.status === 'created' &&
        !draft.pickup_ordered
    )

    async function loadExecutePreview(schedule) {
        const requestNumber = ++executePreviewRequest.current
        setExecutePreview({ loading: true })
        try {
            const token = await getToken()
            const params = draft.courier === 'apaczka' && schedule
                ? `?${new URLSearchParams(schedule).toString()}`
                : ''
            const res = await fetch(`/api/shipping/drafts/${draft.id}/execute/preview${params}`, {
                headers: { Authorization: `Bearer ${token}` },
            })
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
            const data = await res.json()
            if (requestNumber === executePreviewRequest.current) setExecutePreview({ data })
        } catch (e) {
            if (requestNumber === executePreviewRequest.current) setExecutePreview({ error: e.message })
        }
    }

    /** Fetch what the courier would receive. Opens the panel first so the click always responds. */
    async function openExecutePreview() {
        const schedule = defaultPickupSchedule(hasFixedApaczkaPickupWindows(draft))
        setExecuteSchedule(schedule)
        await loadExecutePreview(schedule)
    }

    const isSelectable = onToggleSelect && (
        draft.status === 'pending' ||
        draft.status === 'needs_review' ||
        draft.status === 'error' ||
        draft.status === 'created'
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
                        aria-label={`Wybierz przesyłkę ${draft.shopify_order_number || draft.id}`}
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
                    style={{
                        padding: '10px 16px 10px 0', cursor: 'default', display: 'grid', alignItems: 'center',
                        gridTemplateColumns: columnGridTemplate
                    }}
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
                                        : draft.status === 'pending_confirmation' ? (T.sh_status_pending_confirmation ?? 'oczekuje na potwierdzenie')
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
                                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', margin: '4px 0 6px' }}>
                                    <Pill kind={matchStatusPillKind(draft.shipping_service_match_status)}>
                                        {matchStatusLabel(draft.shipping_service_match_status)}
                                    </Pill>
                                    {draft.shipping_service_match_source && (
                                        <span className="dim" style={{ fontSize: '0.85em' }}>
                                            Źródło: {draft.shipping_service_match_source}
                                        </span>
                                    )}
                                </div>
                                {draft.pickup_point?.id && (
                                    <div style={{ margin: '4px 0 8px', fontSize: '0.88em' }}>
                                        <span className="dim">Punkt: </span>
                                        <span className="mono">{draft.pickup_point.id}</span>
                                        {draft.pickup_point.name && (
                                            <span className="dim"> · {draft.pickup_point.name}</span>
                                        )}
                                    </div>
                                )}
                                {showApaczkaServiceEditor ? (
                                    <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
                                        <select
                                            value={selectedApaczkaService}
                                            onChange={e => setApaczkaServiceEdit({
                                                baseline: apaczkaServiceBaseline,
                                                value: e.target.value,
                                            })}
                                            disabled={isBusy}
                                        >
                                            <option value="">{T.sh_apaczka_service_placeholder ?? '— wybierz serwis —'}</option>
                                            {apaczkaServices.map(s => (
                                                <option key={s.service_id} value={s.service_id}>{s.label}</option>
                                            ))}
                                        </select>
                                        <button
                                            className="btn btn-secondary"
                                            disabled={
                                                isBusy ||
                                                !selectedApaczkaService ||
                                                selectedApaczkaService === draft.apaczka_service_id
                                            }
                                            onClick={() => onSetApaczkaService(draft, selectedApaczkaService)}
                                        >
                                            {isBusy
                                                ? (T.sh_apaczka_service_save_busy ?? 'Zapisywanie…')
                                                : (T.sh_apaczka_service_save ?? 'Zapisz')}
                                        </button>
                                    </div>
                                ) : (
                                    <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
                                        <strong>{matchedApaczkaService.label}</strong>
                                        {canManage && (
                                            <button
                                                className="btn btn-ghost"
                                                onClick={() => setEditingOverride({
                                                    baseline: matchStatusBaseline,
                                                    value: true,
                                                })}
                                                disabled={isBusy}
                                            >
                                                Zmień
                                            </button>
                                        )}
                                    </div>
                                )}
                            </div>
                        )}

                        {draft.source === 'allegro' && (
                            <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
                                <div className="detail-label">Faktura Fakturownia</div>
                                {localInvoiceId && localInvoiceId !== 'pending' && !draft.fakturownia_invoice_error ? (
                                    <div style={{ marginTop: 4, color: 'var(--ok, #16a34a)', display: 'flex', alignItems: 'center', gap: 6 }}>
                                        <Icon name="check" size={14} />
                                        <span>Faktura #{localInvoiceId}</span>
                                    </div>
                                ) : (
                                    <div style={{ marginTop: 4 }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                                            <span
                                                className={draft.fakturownia_invoice_error ? undefined : 'dim'}
                                                style={{ fontSize: '0.88em', color: draft.fakturownia_invoice_error ? 'var(--error)' : undefined }}
                                            >
                                                {draft.fakturownia_invoice_error
                                                    ? `Automatyzacja wymaga uwagi${localInvoiceId ? ` (faktura #${localInvoiceId})` : ''}`
                                                    : 'Oczekiwanie na automatyczną fakturę'}
                                            </span>
                                            {canManage && (
                                                <button
                                                    className="btn btn-secondary"
                                                    data-testid={`shipping-invoice-${draft.id}`}
                                                    style={{ fontSize: '0.82em', padding: '3px 10px' }}
                                                    onClick={() => setShowInvoicePanel(true)}
                                                >
                                                    <Icon name="invoice" size={12} /> {draft.fakturownia_invoice_error ? 'Sprawdź i ponów' : 'Sprawdź'}
                                                </button>
                                            )}
                                        </div>
                                        {draft.fakturownia_invoice_error && (
                                            <div className="dim" style={{ fontSize: '0.78em', marginTop: 4 }}>
                                                {draft.fakturownia_invoice_error}
                                            </div>
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
                                        setInvoiceIdOverride({
                                            baseline: invoiceIdBaseline,
                                            value: result.fakturownia_invoice_id,
                                        })
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
                                    onClick={openExecutePreview}
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
                                    title={draft.courier === 'inpost'
                                        ? 'InPost pobiera etykietę PDF A6'
                                        : 'Otwiera systemowe okno drukowania'}
                                >
                                    <Icon name="printer" size={13} />
                                    {draft.courier === 'inpost' ? 'Drukuj A6' : 'Drukuj etykietę'}
                                </button>
                            )}

                            {draft.status === 'pending_confirmation' && (
                                <button
                                    className="btn btn-secondary"
                                    onClick={() => onConfirmPending(draft)}
                                    disabled={isBusy}
                                    title={draft.courier === 'inpost'
                                        ? 'InPost nadał numer przesyłki, czeka na numer śledzenia — sprawdzane automatycznie co 5s, albo kliknij żeby sprawdzić od razu'
                                        : 'Allegro jeszcze przetwarza tę przesyłkę — sprawdzane automatycznie co 5s, albo kliknij żeby sprawdzić od razu'}
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
                                title="Zamów podjazd kuriera"
                                onCancel={() => setPickupModal(null)}
                                onConfirm={schedule => {
                                    setPickupModal(null)
                                    onPickup(draft, schedule)
                                }}
                            />
                        )}

                        {executePreview && (
                            <PickupScheduleModal
                                title="Sprawdź, co trafi do kuriera"
                                panelTestId="execute-preview"
                                confirmTestId="execute-preview-confirm"
                                confirmLabel="Wyślij do kuriera"
                                confirmDisabled={!executePreview.data || executePreview.data.preview_available === false}
                                fixedWindows={hasFixedApaczkaPickupWindows(draft)}
                                initialSchedule={executeSchedule}
                                onScheduleChange={draft.courier === 'apaczka' ? schedule => {
                                    setExecuteSchedule(schedule)
                                    loadExecutePreview(schedule)
                                } : undefined}
                                // One pickup control for every carrier. It has to live here
                                // because Apaczka's API has no pickup resource — a collection
                                // can only be requested inside order_send, at execute time. All
                                // three now read this window: Apaczka through _apaczka_call_specs,
                                // Allegro through _order_allegro_pickup, InPost through
                                // create_dispatch_order.
                                withSchedule={true}
                                onCancel={() => {
                                    setExecutePreview(null)
                                    setExecuteSchedule(null)
                                }}
                                onConfirm={schedule => {
                                    const fingerprint = executePreview.data?.fingerprint
                                    setExecutePreview(null)
                                    setExecuteSchedule(null)
                                    onExecute(draft, schedule, fingerprint)
                                }}
                            >
                                <ExecutePreview state={executePreview} />
                            </PickupScheduleModal>
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
    const [bulkExecuteModal, setBulkExecuteModal] = useState(null)
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

    useEffect(() => {
        const timer = window.setTimeout(() => { void load() }, 0)
        return () => window.clearTimeout(timer)
    }, [load])

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
            printPdf(blob, `Etykieta ${draft.shopify_order_number || draft.id}`)
        } catch (e) {
            pushToast({ kind: 'error', msg: `Błąd pobierania etykiety: ${e.message}` })
        }
    }

    function handleExecute(draft, schedule, previewFingerprint) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const requestBody = previewFingerprint
                ? { ...(schedule || {}), preview_fingerprint: previewFingerprint }
                : schedule
            const res = await fetch(`/api/shipping/drafts/${draft.id}/execute`, {
                method: 'POST',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: requestBody ? JSON.stringify(requestBody) : null,
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

    // Auto-poll drafts stuck in pending_confirmation so the operator doesn't have
    // to keep clicking "Sprawdź status". Two carriers land here: an Allegro
    // create-command still IN_PROGRESS, and an InPost shipment that ShipX has
    // accepted but not yet given a tracking number.
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
                    }).catch(() => { })
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

    // Bulk execute is pickup-free for InPost and Allegro: for them a collection is
    // a separate call, so it stays a separate, deliberate button. Apaczka has no
    // standalone pickup endpoint — the collection rides inside order_send — so
    // executing an Apaczka draft always requests a courier, and the only choice
    // left is whether the operator got to pick the window. We therefore refuse to
    // bulk-execute Apaczka until a window is named, rather than shipping an
    // undated COURIER request whose behaviour we have not verified.
    async function handleBulkExecute() {
        const selected = [...selectedDraftIds]
            .map(id => drafts.find(d => d.id === id))
            .filter(Boolean)
        if (selected.some(d => d.courier === 'apaczka')) {
            setBulkExecuteModal({ drafts: selected })
            return
        }
        await runBulkExecute(selected, null)
    }

    async function runBulkExecute(selected, schedule) {
        setBulkProgress({ done: 0, total: selected.length })
        for (let i = 0; i < selected.length; i++) {
            const draft = selected[i]
            // Only Apaczka gets the window — see handleBulkExecute.
            const perDraftSchedule = draft.courier === 'apaczka' ? schedule : null
            try { await handleExecute(draft, perDraftSchedule) } catch { /* error visible in row */ }
            setBulkProgress({ done: i + 1, total: selected.length })
        }
        setBulkProgress(null)
        setSelectedDraftIds(new Set())
        load()
    }


    async function handleBulkPickup(schedule) {
        setBulkPickupModal(false)
        const eligible = [...selectedDraftIds]
            .map(id => drafts.find(d => d.id === id))
            .filter(d => d && (d.courier === 'inpost' || d.courier === 'allegro_delivery') && d.status === 'created' && !d.pickup_ordered)
        setBulkProgress({ done: 0, total: eligible.length })
        for (let i = 0; i < eligible.length; i++) {
            try { await handlePickup(eligible[i], schedule) } catch { /* error visible in row */ }
            setBulkProgress({ done: i + 1, total: eligible.length })
        }
        setBulkProgress(null)
        setSelectedDraftIds(new Set())
        load()
    }

    async function handleBulkPrint() {
        const selected = [...selectedDraftIds]
            .map(id => drafts.find(draft => draft.id === id))
            .filter(draft => draft?.status === 'created' && draft.courier_draft_id)
        if (!selected.length) return

        setBulkProgress({ done: 0, total: selected.length })
        try {
            const token = await getToken()
            const res = await fetch('/api/shipping/labels/batch', {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${token}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ draft_ids: selected.map(draft => draft.id) }),
            })
            if (res.status === 409) {
                const body = await res.json().catch(() => ({}))
                throw new Error(body.detail || 'Co najmniej jedna etykieta nie jest jeszcze gotowa.')
            }
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
            printPdf(await res.blob(), `Etykiety A6 (${selected.length})`)
            setSelectedDraftIds(new Set())
        } catch (error) {
            pushToast({ kind: 'error', msg: `Błąd drukowania etykiet: ${error.message}` })
        } finally {
            setBulkProgress(null)
        }
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
            d.status === 'created')
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
                            <option value="needs_review">{T.sh_status_needs_review ?? 'do sprawdzenia'}</option>
                            <option value="created">{T.sh_status_created ?? 'nadane'}</option>
                            <option value="pending_confirmation">{T.sh_status_pending_confirmation ?? 'oczekuje na potwierdzenie'}</option>
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
                                return d && (d.courier === 'inpost' || d.courier === 'allegro_delivery') && d.status === 'created' && !d.pickup_ordered
                            })
                            const printSelected = [...selectedDraftIds].filter(id => {
                                const d = drafts.find(x => x.id === id)
                                return d && d.status === 'created' && d.courier_draft_id
                            })
                            return (<>
                                {printSelected.length > 0 && (
                                    <button
                                        className="btn btn-secondary"
                                        style={{ fontSize: '0.85em' }}
                                        onClick={handleBulkPrint}
                                        disabled={bulkProgress !== null}
                                        title="Otwiera jedno okno drukowania dla wszystkich zaznaczonych etykiet; etykiety InPost są pobierane jako PDF A6"
                                    >
                                        <Icon name="printer" size={13} />
                                        {bulkProgress !== null
                                            ? `Przygotowuję ${bulkProgress.done}/${bulkProgress.total}…`
                                            : `Drukuj etykiety (${printSelected.length})`}
                                    </button>
                                )}
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
                            key={`${draft.id}:${expandAll ?? 'individual'}`}
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

            {bulkExecuteModal && (() => {
                const apaczkaDrafts = bulkExecuteModal.drafts.filter(d => d.courier === 'apaczka')
                const otherCount = bulkExecuteModal.drafts.length - apaczkaDrafts.length
                const parcelCount = apaczkaDrafts.reduce(
                    (sum, d) => sum + (packagesSortValue(d) || 0), 0
                )
                return (
                    <PickupScheduleModal
                        title="Podjazd dla przesyłek Apaczka"
                        fixedWindows={bulkExecuteModal.drafts.some(hasFixedApaczkaPickupWindows)}
                        panelTestId="bulk-execute-pickup"
                        confirmTestId="bulk-execute-pickup-confirm"
                        confirmLabel="Realizuj zaznaczone"
                        summary={
                            <div data-testid="bulk-execute-scope" style={{ fontSize: '0.85em', display: 'flex', flexDirection: 'column', gap: 4 }}>
                                <div>
                                    Okno dotyczy <strong>{apaczkaDrafts.length}</strong>{' '}
                                    {apaczkaDrafts.length === 1 ? 'przesyłki Apaczka' : 'przesyłek Apaczka'}
                                    {' '}(<strong>{parcelCount}</strong> {parcelCount === 1 ? 'paczka' : 'paczek'}).
                                </div>
                                <div style={{ color: 'var(--text-2)' }}>
                                    Apaczka zamawia kuriera już przy realizacji — tego nie da się
                                    później odwołać bez anulowania przesyłki.
                                </div>
                                {otherCount > 0 && (
                                    <div style={{ color: 'var(--text-2)' }}>
                                        Pozostałe {otherCount} — InPost / Allegro — realizuję bez podjazdu.
                                        Podjazd zamówisz osobno przyciskiem „Zamów podjazd”.
                                    </div>
                                )}
                            </div>
                        }
                        onConfirm={schedule => {
                            const pending = bulkExecuteModal.drafts
                            setBulkExecuteModal(null)
                            runBulkExecute(pending, schedule)
                        }}
                        onCancel={() => setBulkExecuteModal(null)}
                    />
                )
            })()}
        </>
    )
}

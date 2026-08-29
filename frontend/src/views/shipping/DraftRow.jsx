// One row of the shipping table.

import { useState, useRef } from 'react'
import { useT } from '../../lang'
import { Pill } from '../../components/Pill'
import { Icon } from '../../components/Icon'
import { TrackingList } from './TrackingList'
import { PackagesEditor } from './PackagesEditor'
import { RecipientPhone } from './RecipientPhone'
import { ExecutePreview } from './ExecutePreview'
import { InvoicePreviewPanel } from './InvoicePreviewPanel'
import { MaterialTags, PACKAGES_LOCKED_STATUSES } from './MaterialTags'
import { PickupScheduleModal } from './PickupScheduleModal'
import { OrderNumberCell, SourceCell } from './cells'
import { courierLabel, courierPillKind, fmtDate, matchStatusLabel, matchStatusPillKind, pickupOrderIds } from './formatting'
import { defaultPickupSchedule, hasFixedApaczkaPickupWindows } from './pickupSchedule'

export function DraftRow({ draft, onPrintLabel, onExecute, onPickup, onMarkFulfilled, onConfirmPending, onSetApaczkaService, onReviewDraft, onSavePackages, onSavePhone, apaczkaServices, busy, canManage, selected, onToggleSelect, forceOpen, getToken, onDraftUpdate, columnGridTemplate, tableMinWidth }) {
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
    const draftPickupOrderIds = pickupOrderIds(draft)
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
                                {draft.cod && (
                                    <>
                                        <div className="detail-label" style={{ marginTop: 10 }}>Pobranie (COD)</div>
                                        <div style={{ fontWeight: 600 }}>
                                            {Number(draft.cod.amount).toFixed(2)} {draft.cod.currency}
                                        </div>
                                    </>
                                )}
                                {draft.cod_error && (
                                    <div style={{ marginTop: 10, color: 'var(--error)', fontSize: '0.88em' }}>
                                        COD: {draft.cod_error}
                                    </div>
                                )}
                            </div>
                            <div>
                                <RecipientPhone
                                    phone={draft.receiver?.phone}
                                    courier={draft.courier}
                                    canEdit={canManage && !PACKAGES_LOCKED_STATUSES.has(draft.status)}
                                    saving={isBusy}
                                    onSave={value => onSavePhone(draft, value)}
                                />
                                <div style={{ marginTop: 10 }}>
                                    <TrackingList draft={draft} />
                                </div>
                                <div className="detail-label" style={{ marginTop: 10 }}>ID draftu kuriera</div>
                                <div className="mono dim">{draft.courier_draft_id || '—'}</div>
                                <div className="detail-label" style={{ marginTop: 10 }}>ID zlecenia odbioru</div>
                                <div>
                                    {draftPickupOrderIds.length
                                        ? draftPickupOrderIds.map(id => (
                                            <div key={id} className="mono copyable" title="Kliknij żeby skopiować"
                                                onClick={() => navigator.clipboard.writeText(id)}
                                                style={{ cursor: 'pointer' }}>
                                                {id}
                                            </div>
                                        ))
                                        : <span className="dim">—</span>}
                                </div>
                            </div>
                            <div>
                                <PackagesEditor
                                    breakdown={draft.packages_breakdown}
                                    canEdit={canManage && !PACKAGES_LOCKED_STATUSES.has(draft.status)}
                                    saving={isBusy}
                                    onSave={rows => onSavePackages(draft, rows)}
                                />
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

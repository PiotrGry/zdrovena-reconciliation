// The shipping screen: composition only.
//
// This file used to be 2047 lines holding the table, sync, sorting, statuses,
// preview/execution, invoice UX, PDF printing, modals and provider-specific
// presentation all at once (issue #318). Each of those now lives beside it in
// ./shipping and is re-exported here only where a test or another view already
// imported it from this path.

import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Icon } from '../components/Icon'
import { Pill } from '../components/Pill'
import { useToast } from '../components/Toast'
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
import { DraftRow } from './shipping/DraftRow'
import { PickupScheduleModal } from './shipping/PickupScheduleModal'
import { defaultPickupSchedule, hasFixedApaczkaPickupWindows } from './shipping/pickupSchedule'
import { apiErrorMessage, syncErrorCount, syncSummary } from './shipping/syncSummary'
import { batchSheetTitle, labelSheetTitle, printPdf, sortValue } from './shipping/formatting'

// Re-exported because ShippingView.test.jsx and older callers import them from
// this module. The implementations live in ./shipping.
export { printPdf, defaultPickupSchedule }

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
            // A silent poll must not open a sign-in prompt — no user gesture here.
            const token = await getToken({ interactive: !silent })
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
            printPdf(blob, labelSheetTitle(draft.shopify_order_number || draft.id))
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

    function handleSavePhone(draft, phone) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ receiver_phone: phone }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się zapisać telefonu')()
    }

    function handleSavePackages(draft, rows) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ packages_breakdown: rows }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się zapisać paczek')()
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
                // Background timer: never interactive.
                const token = await getToken({ interactive: false })
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
            printPdf(await res.blob(), batchSheetTitle())
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
                            onSavePackages={handleSavePackages}
                            onSavePhone={handleSavePhone}
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

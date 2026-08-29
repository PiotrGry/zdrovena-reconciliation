// Choosing the pickup window.

import { useState } from 'react'
import { createPortal } from 'react-dom'
import { useT } from '../../lang'
import { APACZKA_PICKUP_WINDOWS, TIME_SLOTS, addHours, defaultPickupSchedule, toMinutes } from './pickupSchedule'

export function PickupScheduleModal({
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

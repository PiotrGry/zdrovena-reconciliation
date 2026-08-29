// Pickup window arithmetic.


export const TIME_SLOTS = ['07:00', '08:00', '09:00', '10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '16:00', '17:00', '18:00']
export const APACZKA_PICKUP_WINDOWS = [
    ['09:00', '17:00'],
    ['11:00', '14:00'],
    ['14:00', '17:00'],
]

export function hasFixedApaczkaPickupWindows(draft) {
    return draft.courier === 'apaczka' && draft.apaczka_service_id === '23'
}

export function toMinutes(t) { const [h, m] = t.split(':').map(Number); return h * 60 + m }
export function addHours(t, hrs) {
    const m = toMinutes(t) + hrs * 60
    return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
}

export function nextCalendarDate(dateString) {
    const date = new Date(`${dateString}T00:00:00Z`)
    date.setUTCDate(date.getUTCDate() + 1)
    return date.toISOString().slice(0, 10)
}

export function defaultPickupSchedule(fixedWindows = false) {
    const now = new Date()
    const today = now.toISOString().slice(0, 10)
    const minFromToday = addHours(`${String(now.getHours()).padStart(2, '0')}:00`, 2)
    if (fixedWindows) {
        const remainingWindow = APACZKA_PICKUP_WINDOWS.find(([start]) => start >= minFromToday)
        const [from, to] = remainingWindow || APACZKA_PICKUP_WINDOWS[0]
        return {
            pickup_date: remainingWindow ? today : nextCalendarDate(today),
            pickup_from: from,
            pickup_to: to,
        }
    }
    const from = TIME_SLOTS.find(t => t >= minFromToday && t <= '16:00') || '09:00'
    return { pickup_date: today, pickup_from: from, pickup_to: addHours(from, 2) }
}

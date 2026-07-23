import { useEffect, useRef } from 'react'

// Odpytywanie listy w tle, świadome widoczności karty.
//
//  - Strzela tylko gdy karta jest widoczna (document.visibilityState === 'visible'),
//    więc nieaktywne karty nie generują ruchu do API.
//  - Natychmiast odświeża po powrocie do karty (zdarzenie 'visibilitychange'),
//    dzięki czemu operator nie czeka pełnego interwału po przełączeniu z Allegro/Shopify.
//  - callback trzymany w ref, żeby zmiana referencji funkcji nie restartowała interwału.
export function usePolling(callback, intervalMs, { enabled = true } = {}) {
    const cbRef = useRef(callback)

    useEffect(() => {
        cbRef.current = callback
    }, [callback])

    useEffect(() => {
        if (!enabled) return
        function tick() {
            if (document.visibilityState === 'visible') cbRef.current()
        }
        const id = setInterval(tick, intervalMs)
        document.addEventListener('visibilitychange', tick)
        return () => {
            clearInterval(id)
            document.removeEventListener('visibilitychange', tick)
        }
    }, [intervalMs, enabled])
}

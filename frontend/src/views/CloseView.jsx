import { useState, useCallback, useEffect } from 'react'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { CloseHero } from './close/CloseHero'
import { DocChecklist } from './close/DocChecklist'
import { RunControls } from './close/RunControls'
import { RunPanel } from './close/RunPanel'
import { ResultPanel } from './close/ResultPanel'
import { CloseHistoryTable } from './close/CloseHistoryTable'
import { MONTHS_PL } from '../data'

/**
 * Zamknięcie miesiąca — inline single-page layout.
 *
 * Sekwencja sekcji (od góry):
 *   1. CloseHero — duży tytuł miesiąca + status + ostatnie zamknięcie
 *   2. DocChecklist — 6 wymaganych dokumentów + collapsible inbox
 *   3. RunControls — wybór miesiąca, dry-run, vendorzy, CTA
 *   4. RunPanel — kroki + logi side-by-side (tylko running/error/done)
 *   5. ResultPanel — metryki wyniku (tylko done)
 *   6. CloseHistoryTable — historia ostatnich zamknięć
 */
export default function CloseView() {
    useT() // lang context needed for child components
    const { getToken } = useAuth()

    // Domyślnie poprzedni miesiąc (ten do zamknięcia)
    const now = new Date()
    const defaultMonth = now.getMonth() === 0 ? 12 : now.getMonth()
    const defaultYear = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear()

    const [year, setYear] = useState(defaultYear)
    const [month, setMonth] = useState(defaultMonth)
    const [dryRun, setDryRun] = useState(true)
    const [running, setRunning] = useState(false)
    const [status, setStatus] = useState('ready')
    const [preCompleted, setPreCompleted] = useState([])
    const [ignoredVendors, setIgnoredVendors] = useState([])
    const [inboxReady, setInboxReady] = useState(null) // null=loading, true=ok, false=missing
    const [hasResult, setHasResult] = useState(false)
    const [resultData, setResultData] = useState(null)
    const [runProgress, setRunProgress] = useState(0)
    const [runKey, setRunKey] = useState(0)
    const [runDryRun, setRunDryRun] = useState(true) // dry setting dla bieżącego runu (nie zmienia checkboxa)
    const [historyKey, setHistoryKey] = useState(0)
    const [lastClose, setLastClose] = useState(null)

    const yearOptions = [defaultYear - 1, defaultYear, defaultYear + 1]
        .filter(y => y <= now.getFullYear())

    const toggleVendor = useCallback((v) => {
        setIgnoredVendors(prev => prev.includes(v) ? prev.filter(x => x !== v) : [...prev, v])
    }, [])

    // Zmiana miesiąca/roku resetuje stan biegu i wyniku
    useEffect(() => {
        setInboxReady(null)
        setRunning(false)
        setStatus('ready')
        setHasResult(false)
        setResultData(null)
    }, [month, year])

    // Załaduj checkpoint state dla wybranego miesiąca
    const loadState = useCallback(async () => {
        try {
            const token = await getToken()
            const res = await fetch(`/api/close/state?year=${year}&month=${month}`, {
                headers: { Authorization: `Bearer ${token}` },
            })
            if (res.ok) {
                const data = await res.json()
                setPreCompleted(data.completed_steps ?? [])
            }
        } catch {
            /* ignore */
        }
    }, [getToken, year, month])

    useEffect(() => { loadState() }, [loadState])

    // Załaduj informacje o ostatnim zamknięciu (do hero)
    useEffect(() => {
        let cancelled = false
        const loadLast = async () => {
            try {
                const token = await getToken()
                const res = await fetch('/api/close/history?limit=1', {
                    headers: { Authorization: `Bearer ${token}` },
                })
                if (!res.ok) return
                const data = await res.json()
                if (cancelled || !data?.length) return
                const h = data[0]
                setLastClose({
                    ts: h.ts,
                    monthName: h.month_name ?? MONTHS_PL[(h.month ?? 1) - 1],
                    year: h.year,
                    status: h.status,
                })
            } catch {
                /* ignore */
            }
        }
        loadLast()
        return () => { cancelled = true }
    }, [getToken, historyKey])

    // Decyzja: czy CTA jest aktywne i jaki jest powód blokady
    const canRun = inboxReady === true && !running
    const runReason = running ? null
        : inboxReady === null ? 'Sprawdzam dokumenty…'
        : inboxReady === false ? 'Uzupełnij brakujące dokumenty w checklist powyżej'
        : null

    // Status pochodny dla hero (uwzględnia inboxReady)
    const heroStatus = running
        ? 'running'
        : status === 'error'
            ? 'error'
            : status === 'done'
                ? 'done'
                : !inboxReady
                    ? 'blocked'
                    : 'ready'

    const start = (forceDry = null) => {
        // forceDry overrides checkbox (e.g. "Sprawdź dry-run") without changing dryRun state
        setRunDryRun(forceDry ?? dryRun)
        setStatus('ready')
        setHasResult(false)
        setResultData(null)
        setRunProgress(0)
        setRunning(true)
        setRunKey(k => k + 1)
    }

    const handleDone = (s, data) => {
        setStatus(s)
        setRunning(false)
        if (s === 'done' || s === 'error') {
            setHasResult(true)
            setResultData(data)
            setHistoryKey(k => k + 1) // odśwież historię zawsze po zakończeniu
        }
        if (s === 'done') loadState()
    }


    return (
        <div className="close-view">
            <CloseHero
                year={year}
                month={month}
                status={heroStatus}
                progress={running ? runProgress : null}
                lastClose={lastClose}
                isDryRun={running || hasResult ? runDryRun : dryRun}
            />

            <DocChecklist onStatusChange={setInboxReady} />

            <RunControls
                year={year}
                month={month}
                onYearChange={setYear}
                onMonthChange={setMonth}
                yearOptions={yearOptions}
                dryRun={dryRun}
                onDryRunChange={setDryRun}
                ignoredVendors={ignoredVendors}
                onToggleVendor={toggleVendor}
                canRun={canRun}
                runReason={runReason}
                running={running}
                hasResult={hasResult}
                preCompleted={preCompleted}
                onRun={() => start()}
                onDryCheck={() => start(true)}
            />

            {(running || hasResult) && (
                <RunPanel
                    key={runKey}
                    year={year}
                    month={month}
                    dryRun={runDryRun}
                    preCompleted={preCompleted}
                    ignoredVendors={ignoredVendors}
                    onProgressChange={setRunProgress}
                    onDone={handleDone}
                />
            )}

            {hasResult && status === 'done' && resultData && (
                <ResultPanel result={resultData} />
            )}

            <CloseHistoryTable refreshKey={historyKey} />
        </div>
    )
}

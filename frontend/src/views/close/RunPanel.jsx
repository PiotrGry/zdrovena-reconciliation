import { useState, useRef, useCallback, useEffect } from 'react'
import { useAuth } from '../../auth'
import { Icon } from '../../components/Icon'
import { PIPELINE_STEPS, normalizeStepKey } from '../../data'

const STEP_EST_MS = [2000, 1000, 5000, 8000, 12000, 2000, 4000, 3000]

function stepStateClass(state) {
    if (state === 'running') return 'running'
    if (state === 'done') return 'done'
    if (state === 'error') return 'error'
    return 'pending'
}

/**
 * Side-by-side panel: lewa kolumna 240px ze stanami kroków, prawa kolumna
 * pełnoszerokim ciemnym terminalem z logami. Auto-scroll logów. Renderowany
 * tylko gdy status === 'running' | 'error' | 'done'.
 */
export function RunPanel({
    year,
    month,
    dryRun,
    preCompleted = [],
    ignoredVendors = [],
    onProgressChange,
    onDone,
}) {
    const { getToken } = useAuth()
    const [states, setStates] = useState(() =>
        PIPELINE_STEPS.map(s =>
            preCompleted.map(normalizeStepKey).includes(s.key) ? 'done' : 'pending'
        )
    )
    const [logs, setLogs] = useState(() =>
        preCompleted
            .map(k => {
                const step = PIPELINE_STEPS.find(s => s.key === k)
                return step ? { ts: '—', msg: `✓ ${step.title} (checkpoint)`, kind: 'ok' } : null
            })
            .filter(Boolean)
    )
    const [status, setStatus] = useState('running')
    const abortRef = useRef(null)
    const animStoppedRef = useRef(false)
    const startedRef = useRef(false)
    const logBodyRef = useRef(null)

    const addLog = (msg, kind = 'info') =>
        setLogs(prev => [...prev, { ts: new Date().toLocaleTimeString('pl-PL'), msg, kind }])

    // Auto-scroll logów do dołu na każdy nowy wpis
    useEffect(() => {
        if (logBodyRef.current) {
            logBodyRef.current.scrollTop = logBodyRef.current.scrollHeight
        }
    }, [logs])

    // Aktualizuj postęp do hero
    useEffect(() => {
        if (!onProgressChange) return
        const done = states.filter(s => s === 'done').length
        onProgressChange(done / PIPELINE_STEPS.length)
    }, [states, onProgressChange])

    const run = useCallback(async () => {
        abortRef.current = new AbortController()
        animStoppedRef.current = false
        addLog('Uruchamianie pipeline…', 'muted')

        const normalizedPreCompleted = preCompleted.map(normalizeStepKey)
        const animate = async () => {
            for (let i = 0; i < PIPELINE_STEPS.length; i++) {
                if (animStoppedRef.current) break
                if (normalizedPreCompleted.includes(PIPELINE_STEPS[i].key)) continue
                setStates(prev => prev.map((s, idx) => idx === i ? 'running' : s))
                await new Promise(r => setTimeout(r, STEP_EST_MS[i] * 0.4))
                if (!animStoppedRef.current) {
                    setStates(prev => prev.map((s, idx) => idx === i ? 'done' : s))
                }
            }
        }
        animate()

        try {
            const token = await getToken()
            const res = await fetch('/api/close', {
                method: 'POST',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ year, month, dry_run: dryRun, ignore_vendors: ignoredVendors }),
                signal: abortRef.current.signal,
            })

            animStoppedRef.current = true

            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                const detail = body.detail
                setStates(prev => {
                    const hasRunning = prev.some(s => s === 'running')
                    return prev.map((s, i) => {
                        if (s === 'running') return 'error'
                        if (!hasRunning && i === 0) return 'error'
                        return s
                    })
                })
                if (detail?.blockers) {
                    detail.log_lines?.forEach(line => addLog(line, 'info'))
                    addLog('── Błąd pipeline ──', 'err')
                    detail.blockers.forEach(d => addLog(`  ❌ ${d}`, 'err'))
                    setStatus('error')
                    onDone?.('error', null)
                    return
                }
                const msg = Array.isArray(detail)
                    ? detail.join(', ')
                    : (typeof detail === 'string' ? detail : `HTTP ${res.status}`)
                addLog(`❌ ${msg}`, 'err')
                setStatus('error')
                onDone?.('error', null)
                return
            }

            const data = await res.json()
            data.log_lines?.forEach(line => addLog(line, 'info'))

            const allCompleted = new Set(
                [...(preCompleted ?? []), ...(data.steps_completed ?? [])].map(normalizeStepKey)
            )
            setStates(PIPELINE_STEPS.map(s => {
                if (allCompleted.has(s.key)) return 'done'
                if (data.has_critical_errors) return 'error'
                return 'pending'
            }))

            addLog(
                `Pipeline zakończony. Faktury: ${data.sales_invoice_count}, brutto: ${data.sales_gross_total}`,
                data.has_critical_errors ? 'err' : 'ok'
            )

            const finalStatus = data.has_critical_errors ? 'error' : 'done'
            setStatus(finalStatus)
            onDone?.(finalStatus, data)
        } catch (e) {
            animStoppedRef.current = true
            if (e.name === 'AbortError') {
                setStates(prev => prev.map(s => s === 'running' ? 'pending' : s))
                addLog('Pipeline przerwany.', 'muted')
                setStatus('ready')
                onDone?.('ready', null)
            } else {
                setStates(prev => {
                    const hasRunning = prev.some(s => s === 'running')
                    return prev.map((s, i) => {
                        if (s === 'running') return 'error'
                        if (!hasRunning && i === 0) return 'error'
                        return s
                    })
                })
                addLog(`Błąd: ${e.message}`, 'err')
                setStatus('error')
                onDone?.('error', null)
            }
        }
    }, [year, month, dryRun, getToken, onDone, preCompleted, ignoredVendors])

    // Uruchom raz przy montowaniu (komponent jest re-mountowany via runKey z parenta)
    useEffect(() => {
        if (!startedRef.current) {
            startedRef.current = true
            run()
        }
    }, [run])

    const abort = () => abortRef.current?.abort()

    return (
        <section className="card run-panel" aria-labelledby="run-panel-title">
            <div className="card-head">
                <span className="card-title" id="run-panel-title">
                    <Icon name="play" size={14} /> Pipeline
                </span>
                <div className="card-head-actions">
                    <span className="card-sub">{logs.length} log{logs.length === 1 ? '' : 'ów'}</span>
                    {status === 'running' && (
                        <button type="button" className="btn btn-ghost btn-sm" onClick={abort}>
                            <Icon name="x" size={13} /> Przerwij
                        </button>
                    )}
                </div>
            </div>
            <div className="run-panel-grid">
                <ol className="run-panel-steps" aria-label="Kroki pipeline">
                    {PIPELINE_STEPS.map((step, i) => (
                        <li key={step.n} className="step" data-state={stepStateClass(states[i])}>
                            <div className="step-num" aria-hidden="true">
                                {states[i] === 'done' ? <Icon name="check" size={11} /> : step.n}
                            </div>
                            <div className="step-title">{step.title}</div>
                            <div className="step-duration">
                                {states[i] === 'done' ? '✓' : step.est}
                            </div>
                        </li>
                    ))}
                </ol>
                <div className="run-panel-logs" ref={logBodyRef} aria-live="polite" aria-label="Logi pipeline">
                    {logs.map((l, i) => (
                        <div key={i} className={`log-line ${l.kind}`}>
                            <span className="log-time">{l.ts}</span>
                            <span>{l.msg}</span>
                        </div>
                    ))}
                </div>
            </div>
        </section>
    )
}

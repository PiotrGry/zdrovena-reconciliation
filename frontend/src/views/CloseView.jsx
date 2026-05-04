import { useState, useRef, useCallback, useEffect } from 'react'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Icon } from '../components/Icon'
import { Pill } from '../components/Pill'
import { PIPELINE_STEPS, MONTHS_PL } from '../data'

const STEP_EST_MS = [2000, 1000, 5000, 8000, 12000, 2000, 4000, 3000]

function stepStateClass(state) {
    if (state === 'running') return 'running'
    if (state === 'done') return 'done'
    if (state === 'error') return 'error'
    return 'pending'
}

function ResultSummary({ result, T }) {
    if (!result) return null
    return (
        <div className="card">
            <div className="card-head">
                <span className="card-title"><Icon name="check" size={14} /> {T.close_result}</span>
            </div>
            <div style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', fontSize: 13 }}>
                    <span><strong>{result.sales_invoice_count}</strong> {T.close_sales_count}</span>
                    <span><strong>{result.cost_invoice_count}</strong> {T.close_cost_count}</span>
                    <span><strong>{result.sales_gross_total}</strong> brutto</span>
                    <Pill kind={result.email_sent ? 'ok' : 'default'}>
                        {result.email_sent ? T.close_email_sent : T.close_email_pending}
                    </Pill>
                </div>
                {result.warnings?.length > 0 && (
                    <div>
                        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--warning)', marginBottom: 4 }}>
                            ⚠ {T.close_warnings} ({result.warnings.length})
                        </div>
                        {result.warnings.map((w, i) => (
                            <div key={i} style={{ fontSize: 12, color: 'var(--text-2)', paddingLeft: 12 }}>· {w}</div>
                        ))}
                    </div>
                )}
                {result.errors?.length > 0 && (
                    <div>
                        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--err, #e53e3e)', marginBottom: 4 }}>
                            ✖ {T.close_errors_label} ({result.errors.length})
                        </div>
                        {result.errors.map((e, i) => (
                            <div key={i} style={{ fontSize: 12, color: 'var(--text-2)', paddingLeft: 12 }}>· {e}</div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    )
}

export function CloseRunner({ year, month, dryRun, preCompleted = [], onDone }) {
    const { getToken } = useAuth()
    const { t, lang } = useT()
    const T = t[lang]
    const [states, setStates] = useState(() =>
        PIPELINE_STEPS.map(s => preCompleted.includes(s.key) ? 'done' : 'pending')
    )
    const [logs, setLogs] = useState(() =>
        preCompleted.map(k => {
            const step = PIPELINE_STEPS.find(s => s.key === k)
            return step ? { ts: '—', msg: `✓ ${step.title} (checkpoint)`, kind: 'ok' } : null
        }).filter(Boolean)
    )
    const [status, setStatus] = useState('running')
    const [result, setResult] = useState(null)
    const abortRef = useRef(null)
    const animStoppedRef = useRef(false)

    const addLog = (msg, kind = 'info') =>
        setLogs(prev => [...prev, { ts: new Date().toLocaleTimeString('pl-PL'), msg, kind }])

    const run = useCallback(async () => {
        abortRef.current = new AbortController()
        animStoppedRef.current = false
        addLog('Uruchamianie pipeline…', 'muted')

        // UX animation runs concurrently with the API call
        const animate = async () => {
            for (let i = 0; i < PIPELINE_STEPS.length; i++) {
                if (animStoppedRef.current) break
                if (preCompleted.includes(PIPELINE_STEPS[i].key)) continue
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
                body: JSON.stringify({ year, month, dry_run: dryRun }),
                signal: abortRef.current.signal,
            })

            animStoppedRef.current = true

            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                const detail = body.detail
                // 422 — pre-flight blockers: {blockers: [...], log_lines: [...]}
                if (res.status === 422 && detail?.blockers) {
                    setStates(prev => prev.map(s => s === 'running' ? 'error' : s))
                    detail.log_lines?.forEach(line => addLog(line, 'info'))
                    addLog('── Brakujące dokumenty ──', 'err')
                    detail.blockers.forEach(d => addLog(`  • ${d}`, 'err'))
                    setStatus('error')
                    onDone?.('error', null)
                    return
                }
                throw new Error(Array.isArray(detail) ? detail.join(', ') : (detail ?? `HTTP ${res.status}`))
            }

            const data = await res.json()

            // Show full CLI output in log panel
            data.log_lines?.forEach(line => addLog(line, 'info'))

            // Reconcile actual completed steps from API response
            const allCompleted = new Set([...(preCompleted ?? []), ...(data.steps_completed ?? [])])
            setStates(PIPELINE_STEPS.map(s => {
                if (allCompleted.has(s.key)) return 'done'
                if (data.has_critical_errors) return 'error'
                return 'pending'
            }))

            setResult(data)
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
                setStates(prev => prev.map(s => s === 'running' ? 'error' : s))
                addLog(`Błąd: ${e.message}`, 'err')
                setStatus('error')
                onDone?.('error', null)
            }
        }
    }, [year, month, dryRun, getToken, onDone, preCompleted])

    const abort = () => abortRef.current?.abort()

    const started = useRef(false)
    if (!started.current) { started.current = true; run() }

    return (
        <div className="close-body">
            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="play" size={14} /> Kroki pipeline</span>
                </div>
                <div className="steps" style={{ padding: '4px 16px 12px' }}>
                    {PIPELINE_STEPS.map((step, i) => (
                        <div key={step.n} className="step" data-state={stepStateClass(states[i])}>
                            <div className="step-num">{states[i] === 'done' ? <Icon name="check" size={11} /> : step.n}</div>
                            <div>
                                <div className="step-title">{step.title}</div>
                            </div>
                            <div className="step-duration">{states[i] === 'running' ? step.est : states[i] === 'done' ? '✓' : step.est}</div>
                        </div>
                    ))}
                </div>
                {status === 'running' && (
                    <div style={{ padding: '10px 16px', borderTop: '1px solid var(--border)' }}>
                        <button className="btn btn-ghost btn-sm" onClick={abort}>
                            <Icon name="x" size={13} /> Przerwij
                        </button>
                    </div>
                )}
            </div>

            {result && <ResultSummary result={result} T={T} />}

            <div className="card log-card">
                <div className="card-head">
                    <span className="card-title"><Icon name="eye" size={13} /> Logi</span>
                    <span className="card-sub">{logs.length} wpisów</span>
                </div>
                <div className="log-body">
                    {logs.map((l, i) => (
                        <div key={i} className={`log-line ${l.kind}`}>
                            <span className="log-time">{l.ts}</span>
                            <span>{l.msg}</span>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    )
}

export function CloseModal({ open, onClose, onDone: onDoneExternal }) {
    const { t, lang } = useT()
    const { getToken } = useAuth()
    const T = t[lang]
    const [dryRun, setDryRun] = useState(true)
    const [running, setRunning] = useState(false)
    const [status, setStatus] = useState('ready')
    const [preCompleted, setPreCompleted] = useState([])
    const [year, setYear] = useState(() => new Date().getFullYear())
    const [month, setMonth] = useState(() => new Date().getMonth() + 1)

    const years = [new Date().getFullYear() - 1, new Date().getFullYear()]

    useEffect(() => {
        if (!open) return
        const fetchState = async () => {
            try {
                const token = await getToken()
                const res = await fetch(
                    `/api/close/state?year=${year}&month=${month}`,
                    { headers: { Authorization: `Bearer ${token}` } }
                )
                if (res.ok) {
                    const data = await res.json()
                    setPreCompleted(data.completed_steps ?? [])
                }
            } catch {
                // brak state — nie blokuj
            }
        }
        fetchState()
    }, [open, year, month, getToken])

    useEffect(() => {
        if (!open) { setRunning(false); setStatus('ready') }
    }, [open])

    if (!open) return null

    const start = () => setRunning(true)
    const done = (s) => { setStatus(s); setRunning(false); onDoneExternal?.(s) }

    return (
        <div className="modal-backdrop open" onClick={onClose}>
            <div className="modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 860, width: '92vw' }}>
                <div className="modal-head">
                    <div className="modal-eyebrow">{T.close_step} · {MONTHS_PL[month - 1]} {year}</div>
                    <h2 className="modal-title">{T.close_title}</h2>
                </div>
                <div className="modal-body" style={{ padding: '18px 26px' }}>
                    {!running ? (
                        <>
                            {status === 'ready' && (
                                <div style={{ display: 'flex', gap: 12, marginBottom: 16, alignItems: 'center' }}>
                                    <label style={{ fontSize: 13, color: 'var(--text-2)' }}>{T.close_month}</label>
                                    <select
                                        className="filter-btn"
                                        value={month}
                                        onChange={e => setMonth(Number(e.target.value))}
                                        style={{ padding: '4px 8px' }}
                                    >
                                        {MONTHS_PL.map((m, i) => (
                                            <option key={i + 1} value={i + 1}>{m}</option>
                                        ))}
                                    </select>
                                    <select
                                        className="filter-btn"
                                        value={year}
                                        onChange={e => setYear(Number(e.target.value))}
                                        style={{ padding: '4px 8px' }}
                                    >
                                        {years.map(y => <option key={y} value={y}>{y}</option>)}
                                    </select>
                                </div>
                            )}
                            <div className="steps">
                                {preCompleted.length > 0 && (
                                    <div style={{ marginBottom: 10, fontSize: 12, color: 'var(--text-3)', display: 'flex', alignItems: 'center', gap: 6 }}>
                                        <Icon name="check" size={12} /> {preCompleted.length} z {PIPELINE_STEPS.length} kroków ukończonych z poprzedniego runu (checkpoint)
                                    </div>
                                )}
                                {PIPELINE_STEPS.map(step => (
                                    <div key={step.n} className="step" data-state={preCompleted.includes(step.key) ? 'done' : 'pending'}>
                                        <div className="step-num">{preCompleted.includes(step.key) ? <Icon name="check" size={11} /> : step.n}</div>
                                        <div>
                                            <div className="step-title">{step.title}</div>
                                        </div>
                                        <div className="step-duration">{preCompleted.includes(step.key) ? '✓' : step.est}</div>
                                    </div>
                                ))}
                            </div>
                        </>
                    ) : (
                        <CloseRunner
                            year={year}
                            month={month}
                            dryRun={dryRun}
                            preCompleted={preCompleted}
                            onDone={done}
                        />
                    )}
                </div>
                <div className="modal-foot">
                    <label className="dry-toggle">
                        <input type="checkbox" checked={dryRun} onChange={e => setDryRun(e.target.checked)} disabled={running} />
                        {T.close_dryrun}
                    </label>
                    <div style={{ display: 'flex', gap: 10 }}>
                        <button className="btn btn-ghost" onClick={onClose}>Zamknij</button>
                        {!running && status !== 'done' && (
                            <button className="btn btn-primary" onClick={start}>
                                <Icon name="play" size={14} /> {T.close_run}
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </div>
    )
}

export default function CloseView() {
    const { t, lang } = useT()
    const { getToken } = useAuth()
    const T = t[lang]
    const [open, setOpen] = useState(false)
    const [lastStatus, setLastStatus] = useState('ready')
    const [completedSteps, setCompletedSteps] = useState([])

    const year = new Date().getFullYear()
    const month = new Date().getMonth() + 1

    const loadState = useCallback(async () => {
        try {
            const token = await getToken()
            const res = await fetch(`/api/close/state?year=${year}&month=${month}`, {
                headers: { Authorization: `Bearer ${token}` },
            })
            if (res.ok) {
                const data = await res.json()
                setCompletedSteps(data.completed_steps ?? [])
            }
        } catch {
            // ignore — non-blocking
        }
    }, [getToken, year, month])

    useEffect(() => { loadState() }, [loadState])

    const statusClass = {
        ready: 'state-ready',
        running: 'state-running',
        done: 'state-done',
        error: 'state-error',
    }[lastStatus] ?? 'state-ready'

    const statusLabel = {
        ready: T.close_status_ready,
        running: T.close_status_running,
        done: T.close_status_done,
        error: T.close_status_error,
    }[lastStatus] ?? ''

    const handleDone = s => {
        setLastStatus(s)
        if (s === 'done') loadState()
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead
                title={T.close_title}
                sub={T.close_sub}
                actions={
                    <button className="btn btn-primary" onClick={() => setOpen(true)}>
                        <Icon name="zap" size={14} /> {T.close_run}
                    </button>
                }
            />

            <div className="close-status-bar">
                <div className="close-state">
                    <span className={`state-badge ${statusClass}`}>{statusLabel}</span>
                    <span className="close-summary">
                        Pipeline 8-stopniowy · {MONTHS_PL[month - 1]} {year}
                    </span>
                </div>
                {completedSteps.length > 0 && (
                    <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                        {completedSteps.length} / {PIPELINE_STEPS.length} kroków ukończonych
                    </span>
                )}
            </div>

            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="play" size={14} /> Kroki pipeline</span>
                </div>
                <div className="steps" style={{ padding: '4px 16px 12px' }}>
                    {PIPELINE_STEPS.map(step => (
                        <div key={step.n} className="step" data-state={completedSteps.includes(step.key) ? 'done' : 'pending'}>
                            <div className="step-num">
                                {completedSteps.includes(step.key) ? <Icon name="check" size={11} /> : step.n}
                            </div>
                            <div>
                                <div className="step-title">{step.title}</div>
                            </div>
                            <div className="step-duration">
                                {completedSteps.includes(step.key) ? '✓' : step.est}
                            </div>
                        </div>
                    ))}
                </div>
            </div>

            <CloseModal open={open} onClose={() => setOpen(false)} onDone={handleDone} />
        </div>
    )
}

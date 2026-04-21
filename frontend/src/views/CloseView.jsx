import { useState, useRef, useCallback, useEffect } from 'react'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Icon } from '../components/Icon'
import { PIPELINE_STEPS } from '../data'

const STEP_EST_MS = [2000, 1000, 5000, 8000, 12000, 2000, 4000, 3000]

function stepStateClass(state) {
    if (state === 'running') return 'running'
    if (state === 'done') return 'done'
    if (state === 'error') return 'error'
    return 'pending'
}

export function CloseRunner({ dryRun, preCompleted = [], onDone }) {
    const { getToken } = useAuth()
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
    const abortRef = useRef(null)

    const addLog = (msg, kind = 'info') =>
        setLogs(prev => [...prev, { ts: new Date().toLocaleTimeString('pl-PL'), msg, kind }])

    const run = useCallback(async () => {
        abortRef.current = new AbortController()
        addLog('Uruchamianie pipeline…', 'muted')

        try {
            const token = await getToken()
            const res = await fetch('/api/close', {
                method: 'POST',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ dry_run: dryRun }),
                signal: abortRef.current.signal,
            })

            if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`)

            // Animate steps — skip already completed
            for (let i = 0; i < PIPELINE_STEPS.length; i++) {
                if (preCompleted.includes(PIPELINE_STEPS[i].key)) continue
                setStates(prev => prev.map((s, idx) => idx === i ? 'running' : s))
                addLog(`Krok ${i + 1}: ${PIPELINE_STEPS[i].title}`, 'info')
                await new Promise(r => setTimeout(r, STEP_EST_MS[i] * 0.4))
                setStates(prev => prev.map((s, idx) => idx === i ? 'done' : s))
                addLog(`✓ ${PIPELINE_STEPS[i].title}`, 'ok')
            }

            const data = await res.json()
            addLog(`Pipeline zakończony. ${data.message ?? ''}`, 'ok')
            setStatus('done')
            onDone?.('done')
        } catch (e) {
            if (e.name === 'AbortError') {
                addLog('Pipeline przerwany.', 'muted')
                setStatus('ready')
                onDone?.('ready')
            } else {
                addLog(`Błąd: ${e.message}`, 'err')
                setStatus('error')
                onDone?.('error')
            }
        }
    }, [dryRun, getToken, onDone, preCompleted])

    const abort = () => abortRef.current?.abort()

    // auto-start
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

    const now = useRef(new Date())

    useEffect(() => {
        if (!open) return
        const fetchState = async () => {
            try {
                const token = await getToken()
                const res = await fetch(
                    `/api/close/state?year=${now.current.getFullYear()}&month=${now.current.getMonth() + 1}`,
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
    }, [open, getToken])

    if (!open) return null

    const start = () => setRunning(true)
    const done = s => { setStatus(s); setRunning(false); onDoneExternal?.(s) }

    return (
        <div className="modal-backdrop open" onClick={onClose}>
            <div className="modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 860, width: '92vw' }}>
                <div className="modal-head">
                    <div className="modal-eyebrow">{T.close_step} · {new Date().toLocaleDateString('pl-PL', { month: 'long', year: 'numeric' })}</div>
                    <h2 className="modal-title">{T.close_title}</h2>
                </div>
                <div className="modal-body" style={{ padding: '18px 26px' }}>
                    {!running && status === 'ready' ? (
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
                    ) : (
                        <CloseRunner dryRun={dryRun} preCompleted={preCompleted} onDone={done} />
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
    const T = t[lang]
    const [open, setOpen] = useState(false)
    const [lastStatus, setLastStatus] = useState('ready')

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
                        Pipeline 8-stopniowy · {new Date().toLocaleDateString('pl-PL', { month: 'long', year: 'numeric' })}
                    </span>
                </div>
                <span style={{ fontSize: 12, color: 'var(--text-3)' }}>Ostatnie uruchomienie: 31 mar 2026</span>
            </div>

            <div className="card">
                <div className="card-head">
                    <span className="card-title"><Icon name="play" size={14} /> Kroki pipeline</span>
                </div>
                <div className="steps" style={{ padding: '4px 16px 12px' }}>
                    {PIPELINE_STEPS.map(step => (
                        <div key={step.n} className="step" data-state="pending">
                            <div className="step-num">{step.n}</div>
                            <div>
                                <div className="step-title">{step.title}</div>
                            </div>
                            <div className="step-duration">{step.est}</div>
                        </div>
                    ))}
                </div>
            </div>

            <CloseModal open={open} onClose={() => { setOpen(false) }} onDone={setLastStatus} />
        </div>
    )
}

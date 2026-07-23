import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchJson } from '../api'
import { useAuth } from '../auth'
import { Icon } from '../components/Icon'
import { useToast } from '../components/Toast'
import { MONTHS_PL } from '../data'
import { CloseHero } from './close/CloseHero'
import { CloseHistoryTable } from './close/CloseHistoryTable'
import { WorkflowBoard } from './close/WorkflowBoard'
import { WorkflowDocuments } from './close/WorkflowDocuments'

export default function CloseView() {
    const { getToken } = useAuth()
    const { pushToast } = useToast()
    const now = useMemo(() => new Date(), [])
    const defaultMonth = now.getMonth() === 0 ? 12 : now.getMonth()
    const defaultYear = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear()
    const [year, setYear] = useState(defaultYear)
    const [month, setMonth] = useState(defaultMonth)
    const [run, setRun] = useState(null)
    const [loading, setLoading] = useState(true)
    const [historyKey, setHistoryKey] = useState(0)
    const [lastClose, setLastClose] = useState(null)

    const yearOptions = [defaultYear - 1, defaultYear, defaultYear + 1]
        .filter(value => value <= now.getFullYear())

    const loadRun = useCallback(async () => {
        setLoading(true)
        try {
            const token = await getToken()
            const data = await fetchJson(`/api/close/workflow?year=${year}&month=${month}`, { token })
            setRun(data)
        } catch (error) {
            pushToast({ kind: 'error', msg: `Nie udało się wczytać workflow: ${error.message}` })
        } finally {
            setLoading(false)
        }
    }, [getToken, month, pushToast, year])

    useEffect(() => {
        const timer = window.setTimeout(() => { void loadRun() }, 0)
        return () => window.clearTimeout(timer)
    }, [loadRun])

    useEffect(() => {
        if (!run?.active_action) return undefined
        const timer = window.setInterval(loadRun, 2000)
        return () => window.clearInterval(timer)
    }, [loadRun, run?.active_action])

    useEffect(() => {
        let cancelled = false
        const loadHistory = async () => {
            try {
                const token = await getToken()
                const history = await fetchJson('/api/close/history?limit=1', { token })
                if (cancelled || !history?.length) return
                const item = history[0]
                setLastClose({
                    ts: item.ts,
                    monthName: item.month_name ?? MONTHS_PL[(item.month ?? 1) - 1],
                    year: item.year,
                    status: item.status,
                })
            } catch {
                // Historia jest pomocnicza; sam dashboard pozostaje użyteczny.
            }
        }
        loadHistory()
        return () => { cancelled = true }
    }, [getToken, historyKey])

    const execute = useCallback(async action => {
        let overrideReason = null
        if (action === 'send' && !window.confirm('Czy paczka została przejrzana i ma zostać wysłana do księgowości?')) {
            return
        }
        if (action === 'send' && run?.issues?.some(issue => issue.severity === 'warning')) {
            overrideReason = window.prompt(
                'Paczka ma ostrzeżenia. Podaj krótki powód świadomej wysyłki:'
            )
            if (!overrideReason?.trim()) return
        }
        setRun(previous => previous ? {
            ...previous,
            status: 'running',
            active_action: action,
            steps: {
                ...previous.steps,
                [action]: {
                    ...(previous.steps?.[action] ?? {}),
                    status: 'running',
                    message: null,
                },
            },
        } : previous)
        try {
            const token = await getToken()
            const data = await fetchJson(`/api/close/workflow/actions/${action}`, {
                method: 'POST',
                token,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    year,
                    month,
                    confirm: action === 'send',
                    override_reason: overrideReason,
                }),
            })
            setRun(data)
            const step = data.steps?.[action]
            pushToast({
                kind: step?.status === 'done' ? 'success' : 'error',
                msg: step?.message ?? 'Etap zakończony.',
            })
            if (action === 'send' && step?.status === 'done') setHistoryKey(key => key + 1)
        } catch (error) {
            pushToast({ kind: 'error', msg: `Nie udało się wykonać etapu: ${error.message}` })
            await loadRun()
        }
    }, [getToken, loadRun, month, pushToast, run?.issues, year])

    const reset = async () => {
        if (!window.confirm('Rozpocząć nowy run dla tego miesiąca? Pobrane pliki nie zostaną usunięte.')) return
        try {
            const token = await getToken()
            const data = await fetchJson('/api/close/workflow/reset', {
                method: 'POST',
                token,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ year, month }),
            })
            setRun(data)
        } catch (error) {
            pushToast({ kind: 'error', msg: `Nie udało się zresetować workflow: ${error.message}` })
        }
    }

    const completedCount = run
        ? Object.values(run.steps ?? {}).filter(step => step.status === 'done').length
        : 0
    const heroStatus = run?.active_action
        ? 'running'
        : run?.status === 'completed'
            ? 'done'
            : run?.status === 'failed'
                ? 'error'
                : run?.status === 'needs_input'
                    ? 'blocked'
                    : 'ready'

    return (
        <div className="close-view">
            <CloseHero
                year={year}
                month={month}
                status={heroStatus}
                progress={run ? completedCount / 7 : 0}
                lastClose={lastClose}
                isDryRun={false}
            />

            <section className="card workflow-period">
                <div className="workflow-period-fields">
                    <label className="field">
                        <span className="field-label">Miesiąc</span>
                        <select value={month} onChange={event => setMonth(Number(event.target.value))}>
                            {MONTHS_PL.map((name, index) => (
                                <option key={name} value={index + 1}>{name}</option>
                            ))}
                        </select>
                    </label>
                    <label className="field">
                        <span className="field-label">Rok</span>
                        <select value={year} onChange={event => setYear(Number(event.target.value))}>
                            {yearOptions.map(value => <option key={value}>{value}</option>)}
                        </select>
                    </label>
                </div>
                <div className="workflow-period-actions">
                    <button type="button" className="btn btn-ghost" onClick={loadRun} disabled={loading}>
                        <Icon name="refresh-cw" size={13} className={loading ? 'spinning' : ''} />
                        Odśwież
                    </button>
                    <button type="button" className="btn btn-ghost" onClick={reset} disabled={run?.active_action}>
                        <Icon name="refresh" size={13} /> Nowy run
                    </button>
                </div>
            </section>

            {run?.issues?.length > 0 && (
                <section className="card workflow-issues" aria-label="Problemy do sprawdzenia">
                    <div className="card-head">
                        <span className="card-title">
                            <Icon name="alert-circle" size={14} /> Problemy i ostrzeżenia
                        </span>
                    </div>
                    <ul>
                        {run.issues.map(issue => (
                            <li key={issue.id} className={`is-${issue.severity}`}>
                                <span>{issue.severity === 'warning' ? 'Ostrzeżenie' : 'Blokada'}</span>
                                {issue.message}
                            </li>
                        ))}
                    </ul>
                </section>
            )}

            {loading && !run && <section className="card workflow-empty">Ładowanie workflow…</section>}

            {run && (
                <>
                    <WorkflowBoard run={run} onAction={execute} />
                    <WorkflowDocuments
                        year={year}
                        month={month}
                        documents={run.documents}
                        onUploaded={() => execute('check')}
                    />
                    {run.logs?.length > 0 && (
                        <details className="card workflow-logs">
                            <summary><Icon name="caret" size={11} /> Log wykonania ({run.logs.length})</summary>
                            <pre>{run.logs.join('\n')}</pre>
                        </details>
                    )}
                </>
            )}

            <CloseHistoryTable refreshKey={historyKey} />
        </div>
    )
}

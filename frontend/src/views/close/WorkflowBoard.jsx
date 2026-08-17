import { Icon } from '../../components/Icon'

const STEPS = [
    {
        id: 'check',
        title: 'Kontrola wstępna',
        description: 'Sprawdź Fakturownię, numerację, źródła kosztów i dokumenty ręczne.',
        action: 'Sprawdź ponownie',
        icon: 'eye',
    },
    {
        id: 'sales',
        title: 'Faktury sprzedażowe',
        description: 'Pobierz faktury sprzedaży i zweryfikuj ciągłość numeracji.',
        action: 'Pobierz sprzedaż',
        icon: 'download',
    },
    {
        id: 'costs',
        title: 'Faktury kosztowe',
        description: 'Pobierz oryginalne załączniki, a brakujące dokumenty sprawdź w Zoho.',
        action: 'Pobierz koszty',
        icon: 'download',
    },
    {
        id: 'reports',
        title: 'JPK i rejestr VAT',
        description: 'Przenieś wgrane raporty i sprawdź kompletność deklaracji.',
        action: 'Sprawdź raporty',
        icon: 'file',
    },
    {
        id: 'bank',
        title: 'Wyciąg bankowy',
        description: 'Przenieś i zweryfikuj wyciąg PKO BP dla wybranego okresu.',
        action: 'Sprawdź wyciąg',
        icon: 'file',
    },
    {
        id: 'package',
        title: 'Paczka księgowa',
        description: 'Zbuduj ZIP dopiero po zakończeniu wszystkich etapów zbierania.',
        action: 'Zbuduj paczkę',
        icon: 'archive',
    },
    {
        id: 'send',
        title: 'Wysyłka',
        description: 'Po przejrzeniu manifestu wyślij paczkę do księgowości.',
        action: 'Wyślij paczkę',
        icon: 'play',
    },
]

const STATUS = {
    pending: { label: 'Oczekuje', className: 'pending', icon: 'clock' },
    running: { label: 'W trakcie', className: 'running', icon: 'refresh-cw' },
    done: { label: 'Gotowe', className: 'done', icon: 'check' },
    failed: { label: 'Wymaga uwagi', className: 'failed', icon: 'alert-circle' },
}

const WAIVED_STATUS = { label: 'Pominięte', className: 'waived', icon: 'slash' }

const COLLECTION_STEPS = ['sales', 'costs', 'reports', 'bank']

// Musi odpowiadać WAIVABLE_STEPS w zdrovena/month_closing/workflow.py — paczki
// i wysyłki nie da się pominąć, bo księgowość dostałaby pusty ZIP.
const WAIVABLE_STEPS = ['check', ...COLLECTION_STEPS]

function isSatisfied(run, stepId) {
    const step = run.steps?.[stepId]
    return step?.status === 'done' || Boolean(step?.waived)
}

function hasActiveBlockers(run) {
    return Boolean(run.issues?.some(
        issue => !issue.waived && ['blocker', 'error'].includes(issue.severity)
    ))
}

function isDisabled(step, run) {
    if (run.active_action) return true
    if (step.id === 'check') return false
    if (COLLECTION_STEPS.includes(step.id)) return !isSatisfied(run, 'check')
    if (step.id === 'package') {
        return !COLLECTION_STEPS.every(id => isSatisfied(run, id)) || hasActiveBlockers(run)
    }
    if (step.id === 'send') return run.steps?.package?.status !== 'done'
    return false
}

export function WorkflowBoard({ run, onAction, onWaive, onUnwaive }) {
    const packageArtifact = run.artifacts?.find(artifact => artifact.kind === 'package')

    return (
        <section className="card workflow-board" aria-labelledby="workflow-board-title">
            <div className="card-head">
                <span className="card-title" id="workflow-board-title">
                    <Icon name="play" size={14} /> Etapy zamknięcia
                </span>
                <span className="card-sub">
                    Run {run.run_id.slice(0, 8)}
                </span>
            </div>

            <div className="workflow-grid">
                {STEPS.map((step, index) => {
                    const stepState = run.steps?.[step.id] ?? { status: 'pending' }
                    const waived = Boolean(stepState.waived)
                    const cfg = waived ? WAIVED_STATUS : STATUS[stepState.status] ?? STATUS.pending
                    const disabled = isDisabled(step, run)
                    const canWaive = WAIVABLE_STEPS.includes(step.id)
                        && (waived || stepState.status === 'failed')
                        && !run.active_action
                    return (
                        <article key={step.id} className={`workflow-step is-${cfg.className}`}>
                            <div className="workflow-step-number">{index + 1}</div>
                            <div className="workflow-step-body">
                                <div className="workflow-step-title-row">
                                    <h3><Icon name={step.icon} size={14} /> {step.title}</h3>
                                    <span className={`workflow-status ${cfg.className}`}>
                                        <Icon
                                            name={cfg.icon}
                                            size={11}
                                            className={stepState.status === 'running' ? 'spinning' : ''}
                                        />
                                        {cfg.label}
                                    </span>
                                </div>
                                <p>{step.description}</p>
                                {stepState.message && (
                                    <div className="workflow-step-message">{stepState.message}</div>
                                )}
                                {waived && (
                                    <div className="workflow-step-message is-waived">
                                        Etap pominięty świadomie — nie blokuje paczki.
                                        Ponowne uruchomienie kasuje pominięcie.
                                    </div>
                                )}
                                {step.id === 'costs' && run.metrics?.cost_found_vendors && (
                                    <div className="workflow-step-meta">
                                        {Object.entries(run.metrics.cost_found_vendors).map(([vendor, source]) => (
                                            <span key={vendor}>{vendor}: {source}</span>
                                        ))}
                                    </div>
                                )}
                                {step.id === 'package' && packageArtifact && (
                                    <>
                                        <div className="workflow-step-meta">
                                            <span>{packageArtifact.key}</span>
                                            <span>{packageArtifact.files?.length ?? 0} plików w manifeście</span>
                                        </div>
                                        <details className="workflow-manifest">
                                            <summary>Pokaż zawartość paczki</summary>
                                            <ul>
                                                {(packageArtifact.files ?? []).map(file => (
                                                    <li key={file}>{file}</li>
                                                ))}
                                            </ul>
                                        </details>
                                    </>
                                )}
                            </div>
                            <div className="workflow-step-actions">
                                {canWaive && (
                                    <button
                                        type="button"
                                        className="btn btn-ghost btn-waive"
                                        onClick={() => (waived ? onUnwaive : onWaive)(`step:${step.id}`)}
                                    >
                                        <Icon name={waived ? 'refresh' : 'slash'} size={13} />
                                        {waived ? 'Przywróć etap' : 'Pomiń mimo problemów'}
                                    </button>
                                )}
                                <button
                                    type="button"
                                    className={step.id === 'send' ? 'btn btn-primary' : 'btn btn-ghost'}
                                    disabled={disabled}
                                    onClick={() => onAction(step.id)}
                                >
                                    {stepState.status === 'running'
                                        ? <Icon name="refresh-cw" size={13} className="spinning" />
                                        : <Icon name={step.icon} size={13} />}
                                    {step.action}
                                </button>
                            </div>
                        </article>
                    )
                })}
            </div>
        </section>
    )
}

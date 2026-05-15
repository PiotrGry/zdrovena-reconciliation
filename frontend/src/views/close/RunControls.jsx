import { Icon } from '../../components/Icon'
import { MONTHS_PL, PIPELINE_STEPS } from '../../data'

const EMAIL_VENDORS = ['Shopify', 'BaseLinker', 'Allegro', 'PayU', 'InPost', 'Apaczka', 'PulsePure', 'Accounting/Bożena']

/**
 * Wybór miesiąca/roku, dry-run, ignorowani vendorzy, przyciski Sprawdź / Uruchom.
 * Cały panel jest disabled gdy running=true.
 */
export function RunControls({
    year,
    month,
    onYearChange,
    onMonthChange,
    yearOptions,
    dryRun,
    onDryRunChange,
    ignoredVendors,
    onToggleVendor,
    canRun,
    runReason,
    running,
    hasResult,
    preCompleted,
    onRun,
    onDryCheck,
}) {
    const resumeCount = preCompleted?.length ?? 0
    const ctaLabel = hasResult ? 'Uruchom ponownie' : 'Uruchom pipeline'

    return (
        <section className="card run-controls" aria-labelledby="run-controls-title">
            <div className="card-head">
                <span className="card-title" id="run-controls-title">
                    <Icon name="play" size={14} /> Uruchomienie
                </span>
            </div>

            {resumeCount > 0 && !hasResult && (
                <div className="banner banner-ok" role="status">
                    <Icon name="refresh-cw" size={13} />
                    <span>
                        <strong>Checkpoint:</strong> {resumeCount}/{PIPELINE_STEPS.length} kroków ukończonych —
                        pipeline wznowi od miejsca, gdzie skończył.
                    </span>
                </div>
            )}

            <div className="run-controls-grid">
                <label className="field">
                    <span className="field-label">Miesiąc</span>
                    <select
                        value={month}
                        onChange={e => onMonthChange(Number(e.target.value))}
                        disabled={running}
                    >
                        {MONTHS_PL.map((m, i) => (
                            <option key={i + 1} value={i + 1}>{m}</option>
                        ))}
                    </select>
                </label>
                <label className="field">
                    <span className="field-label">Rok</span>
                    <select
                        value={year}
                        onChange={e => onYearChange(Number(e.target.value))}
                        disabled={running}
                    >
                        {yearOptions.map(y => (
                            <option key={y} value={y}>{y}</option>
                        ))}
                    </select>
                </label>
                <label className="field field-toggle">
                    <span className="field-label">Tryb</span>
                    <label className="dry-toggle">
                        <input
                            type="checkbox"
                            checked={dryRun}
                            onChange={e => onDryRunChange(e.target.checked)}
                            disabled={running}
                        />
                        Symulacja (dry-run)
                        <span className="field-hint">nic nie zostanie wysłane</span>
                    </label>
                </label>
            </div>

            <details className="run-vendors">
                <summary>
                    <Icon name="caret" size={11} />
                    Pomiń brakujące faktury kosztowe
                    {ignoredVendors.length > 0 && (
                        <span className="run-vendors-count"> · {ignoredVendors.length} pominiętych</span>
                    )}
                </summary>
                <div className="run-vendors-list">
                    {EMAIL_VENDORS.map(v => (
                        <label key={v} className={`vendor-chk${ignoredVendors.includes(v) ? ' is-on' : ''}`}>
                            <input
                                type="checkbox"
                                checked={ignoredVendors.includes(v)}
                                onChange={() => onToggleVendor(v)}
                                disabled={running}
                            />
                            {v}
                        </label>
                    ))}
                </div>
                {ignoredVendors.length > 0 && (
                    <div className="run-vendors-warn">
                        <Icon name="alert-circle" size={12} />
                        {ignoredVendors.length === 1
                            ? '1 dostawca zostanie pominięty'
                            : `${ignoredVendors.length} dostawców zostanie pominiętych`}
                    </div>
                )}
            </details>

            <div className="run-controls-actions">
                <div className="run-controls-hint">
                    {runReason && (
                        <>
                            <Icon name="alert-circle" size={13} />
                            {runReason}
                        </>
                    )}
                </div>
                <div className="run-controls-buttons">
                    {onDryCheck && (
                        <button
                            type="button"
                            className="btn btn-ghost"
                            onClick={onDryCheck}
                            disabled={!canRun}
                        >
                            <Icon name="eye" size={13} /> Sprawdź (dry-run)
                        </button>
                    )}
                    <button
                        type="button"
                        className="btn btn-primary"
                        onClick={onRun}
                        disabled={!canRun}
                        aria-disabled={!canRun}
                        title={runReason ?? ''}
                    >
                        <Icon name="play" size={14} /> {ctaLabel}
                    </button>
                </div>
            </div>
        </section>
    )
}

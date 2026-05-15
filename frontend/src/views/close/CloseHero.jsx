import { Icon } from '../../components/Icon'
import { MONTHS_PL } from '../../data'

const STATUS_LABELS = {
    ready:   { kind: 'ready',    icon: 'play',         label: 'Gotowe do uruchomienia' },
    blocked: { kind: 'blocked',  icon: 'alert-circle', label: 'Brakuje wymaganych plików' },
    running: { kind: 'running',  icon: 'refresh-cw',   label: 'Pipeline w trakcie…' },
    error:   { kind: 'error',    icon: 'x',            label: 'Błąd wykonania' },
    done:    { kind: 'done',     icon: 'check',        label: 'Zamknięto pomyślnie' },
}

/**
 * Status hero — duży nagłówek widoku z nazwą miesiąca, stan pipeline'u
 * po lewej, ostatnie zamknięcie po prawej. W stanie running rośnie pasek
 * postępu na dole karty.
 */
export function CloseHero({
    year,
    month,
    status,
    progress,        // 0..1, opcjonalne, używane tylko gdy status === 'running'
    lastClose,       // { date: ISO, status: 'success'|'partial'|... } | null
    isDryRun,
}) {
    const cfg = STATUS_LABELS[status] ?? STATUS_LABELS.ready
    const monthName = MONTHS_PL[month - 1]

    return (
        <section className="close-hero" aria-labelledby="close-hero-title">
            <div className="close-hero-main">
                <div className="close-hero-eyebrow">Zamknięcie miesiąca</div>
                <h1 id="close-hero-title" className="close-hero-title">
                    {monthName} <span className="close-hero-year">{year}</span>
                </h1>
                <div className="close-hero-meta">
                    <span className={`state-badge state-${cfg.kind}`}>
                        <Icon name={cfg.icon} size={12} className={status === 'running' ? 'spinning' : ''} />
                        {cfg.label}
                    </span>
                    {isDryRun && (
                        <span className="state-badge state-ready" title="Tryb symulacji — nic nie zostanie wysłane">
                            <Icon name="eye" size={12} /> Dry-run
                        </span>
                    )}
                </div>
            </div>
            {lastClose && (
                <div className="close-hero-last" aria-label="Ostatnie zamknięcie">
                    <div className="close-hero-last-label">Ostatnie zamknięcie</div>
                    <div className="close-hero-last-value">
                        {lastClose.monthName} {lastClose.year}
                    </div>
                    <div className="close-hero-last-when">
                        {new Date(lastClose.ts).toLocaleDateString('pl-PL', { day: '2-digit', month: 'short', year: 'numeric' })}
                    </div>
                </div>
            )}
            {status === 'running' && progress != null && (
                <div className="close-hero-progress" role="progressbar"
                    aria-valuenow={Math.round(progress * 100)} aria-valuemin={0} aria-valuemax={100}>
                    <div className="close-hero-progress-bar" style={{ width: `${Math.round(progress * 100)}%` }} />
                </div>
            )}
        </section>
    )
}

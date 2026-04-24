import { useT } from '../lang'
import { PageHead } from '../components/PageHead'
import { Icon } from '../components/Icon'

export default function CostView() {
    const { t, lang } = useT()
    const T = t[lang]

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
            <PageHead title={T.nav_invoices_cost} />
            <div className="card" style={{ padding: '60px 32px', textAlign: 'center', color: 'var(--text-3)' }}>
                <Icon name="archive" size={40} style={{ opacity: 0.3, marginBottom: 16 }} />
                <div className="empty">
                    <div className="h">Faktury kosztowe</div>
                    <div>Moduł w przygotowaniu — integracja z KSeF i skanowaniem OCR.</div>
                </div>
            </div>
        </div>
    )
}

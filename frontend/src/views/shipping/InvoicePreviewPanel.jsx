// Fakturownia invoice preview and creation.

import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '../../components/Icon'
import { createDraftInvoice, getInvoicePreview } from '../../api/endpoints'

export function InvoicePreviewPanel({ draft, getToken, onClose, onCreated }) {
    const [loading, setLoading] = useState(true)
    const [creating, setCreating] = useState(false)
    const [preview, setPreview] = useState(null)
    const [error, setError] = useState(null)
    // R4.3: when the preview total does not match Allegro's "Do zapłaty", block
    // unsafe invoice creation until the operator explicitly acknowledges it.
    const [ackMismatch, setAckMismatch] = useState(false)

    useEffect(() => {
        const ctrl = new AbortController()
        const timer = window.setTimeout(() => {
            if (ctrl.signal.aborted) return

            // R4.3/#135: a fresh preview load (draft change OR reload) must clear
            // prior mismatch acknowledgement — consent must never carry across
            // drafts or across preview versions of the same draft.
            setAckMismatch(false)
            setLoading(true)
            setError(null)
            void getToken().then(token =>
                getInvoicePreview({ draftId: draft.id, token, signal: ctrl.signal })
            ).then(data => {
                if (!ctrl.signal.aborted) { setPreview(data); setLoading(false) }
            }).catch(e => {
                if (e.name !== 'AbortError' && !ctrl.signal.aborted) {
                    setError(e.message)
                    setLoading(false)
                }
            })
        }, 0)
        return () => {
            window.clearTimeout(timer)
            ctrl.abort()
        }
    }, [draft.id, getToken])

    async function handleCreate() {
        setCreating(true)
        setError(null)
        try {
            const token = await getToken()
            const data = await createDraftInvoice({ draftId: draft.id, token })
            onCreated(data)
        } catch (e) {
            setError(e.message)
        } finally {
            setCreating(false)
        }
    }

    return createPortal(
        <div style={{ position: 'fixed', inset: 0, zIndex: 1000, display: 'flex' }}>
            <div onClick={onClose} style={{ flex: 1, background: 'rgba(0,0,0,0.35)' }} />
            <div style={{ width: 500, background: 'var(--bg, #fff)', boxShadow: '-4px 0 24px rgba(0,0,0,0.18)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <h3 style={{ margin: 0, fontSize: '1em', fontWeight: 600 }}>
                        <Icon name="invoice" size={15} style={{ marginRight: 6 }} />
                        Faktura — #{String(draft.shopify_order_number || '').slice(0, 12)}
                    </h3>
                    <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-2)', padding: 4, borderRadius: 4 }}>
                        <Icon name="x" size={18} />
                    </button>
                </div>

                <div style={{ flex: 1, overflow: 'auto', padding: 20 }}>
                    {loading && <div className="dim">Pobieranie danych z Allegro…</div>}
                    {error && <div className="error-banner" style={{ marginBottom: 12 }}><Icon name="alertTriangle" size={13} /> {error}</div>}
                    {preview?.status === 'already_created' && (
                        <div style={{ color: 'var(--ok, #16a34a)' }}>
                            <Icon name="check" size={14} /> Faktura już istnieje (ID: {preview.fakturownia_invoice_id})
                        </div>
                    )}
                    {preview?.status === 'retry_ready' && (
                        <div className="error-banner">
                            <Icon name="alertTriangle" size={13} /> Automatyczne wystawienie faktury nie zostało dokończone: {preview.error}
                        </div>
                    )}
                    {preview?.status === 'preview_ready' && (
                        <>
                            <div style={{ marginBottom: 16 }}>
                                <div className="detail-label">Nabywca</div>
                                <div style={{ fontWeight: 500 }}>{preview.buyer_name}</div>
                                {preview.buyer_company && <div style={{ color: 'var(--text-2)' }}>{preview.buyer_company}</div>}
                                {preview.buyer_nip && <div className="mono dim" style={{ fontSize: '0.85em' }}>NIP: {preview.buyer_nip}</div>}
                                {preview.buyer_email && <div className="dim" style={{ fontSize: '0.85em' }}>{preview.buyer_email}</div>}
                            </div>
                            <div>
                                <div className="detail-label">Pozycje</div>
                                <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 6, fontSize: '0.88em' }}>
                                    <thead>
                                        <tr style={{ borderBottom: '2px solid var(--border)' }}>
                                            <th style={{ textAlign: 'left', padding: '3px 8px 3px 0', color: 'var(--text-3)', fontWeight: 600, fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Nazwa</th>
                                            <th style={{ textAlign: 'center', padding: '3px 8px', color: 'var(--text-3)', fontWeight: 600, fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Szt.</th>
                                            <th style={{ textAlign: 'right', padding: '3px 0 3px 8px', color: 'var(--text-3)', fontWeight: 600, fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Razem brutto</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {preview.positions.map((p, i) => (
                                            <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                                                <td style={{ padding: '6px 8px 6px 0' }}>
                                                    {p.name}
                                                    {p.vat_rate && <span className="dim" style={{ fontSize: '0.8em', marginLeft: 6 }}>VAT {p.vat_rate}</span>}
                                                </td>
                                                <td style={{ padding: '6px 8px', textAlign: 'center' }}>{p.quantity}</td>
                                                <td style={{ padding: '6px 0 6px 8px', textAlign: 'right', fontWeight: 500 }}>{p.line_total.toFixed(2)} zł</td>
                                            </tr>
                                        ))}
                                        {preview.settlement_positions.map((s, i) => (
                                            <tr key={`s${i}`} style={{ borderBottom: '1px solid var(--border)', color: 'var(--text-2)' }}>
                                                <td style={{ padding: '6px 8px 6px 0', fontStyle: 'italic' }}>{s.description}</td>
                                                <td />
                                                <td style={{ padding: '6px 0 6px 8px', textAlign: 'right' }}>{parseFloat(s.amount).toFixed(2)} zł</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                    <tfoot>
                                        <tr style={{ color: 'var(--text-2)', fontSize: '0.92em' }}>
                                            <td colSpan={2} style={{ padding: '8px 8px 2px 0' }}>Suma pozycji</td>
                                            <td style={{ padding: '8px 0 2px 8px', textAlign: 'right' }}>{(preview.positions_total ?? 0).toFixed(2)} zł</td>
                                        </tr>
                                        {(preview.settlement_total ?? 0) > 0 && (
                                            <tr style={{ color: 'var(--text-2)', fontSize: '0.92em' }}>
                                                <td colSpan={2} style={{ padding: '2px 8px 2px 0' }}>Kaucja za opakowania zwrotne</td>
                                                <td style={{ padding: '2px 0 2px 8px', textAlign: 'right' }}>{(preview.settlement_total ?? 0).toFixed(2)} zł</td>
                                            </tr>
                                        )}
                                        <tr>
                                            <td colSpan={2} style={{ padding: '8px 8px 4px 0', fontWeight: 700, fontSize: '1em', borderTop: '2px solid var(--border)' }}>Do zapłaty</td>
                                            <td style={{ padding: '8px 0 4px 8px', textAlign: 'right', fontWeight: 700, fontSize: '1em', borderTop: '2px solid var(--border)' }}>{preview.total_gross.toFixed(2)} zł</td>
                                        </tr>
                                    </tfoot>
                                </table>
                            </div>
                            {preview.allegro_total_to_pay != null && (
                                <div style={{
                                    marginTop: 12, padding: '8px 12px', borderRadius: 6, fontSize: '0.88em',
                                    background: preview.matches_allegro ? 'var(--ok-bg, #f0fdf4)' : 'var(--warn-bg, #fffbeb)',
                                    border: `1px solid ${preview.matches_allegro ? 'var(--ok, #86efac)' : 'var(--warn, #fcd34d)'}`
                                }}>
                                    {preview.matches_allegro
                                        ? <><Icon name="check" size={13} /> Zgadza się z Allegro „Do zapłaty” ({preview.allegro_total_to_pay.toFixed(2)} zł, bez dostawy)</>
                                        : <><Icon name="alertTriangle" size={13} /> Uwaga: różni się od Allegro „Do zapłaty” ({preview.allegro_total_to_pay.toFixed(2)} zł, bez dostawy){preview.difference != null && ` — różnica ${preview.difference > 0 ? '+' : ''}${preview.difference.toFixed(2)} zł`} — sprawdź przed wysłaniem</>
                                    }
                                </div>
                            )}
                            {preview.matches_allegro === false && (
                                <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, fontSize: '0.85em', color: 'var(--warn, #b45309)' }}>
                                    <input type="checkbox" checked={ackMismatch} onChange={e => setAckMismatch(e.target.checked)} />
                                    Rozumiem rozbieżność z Allegro i chcę mimo to utworzyć fakturę
                                </label>
                            )}
                        </>
                    )}
                </div>

                <div style={{ padding: '14px 20px', borderTop: '1px solid var(--border)', display: 'flex', gap: 8 }}>
                    {(preview?.status === 'preview_ready' || preview?.status === 'retry_ready') && (
                        <button className="btn btn-primary" onClick={handleCreate} disabled={creating || (preview.matches_allegro === false && !ackMismatch)}>
                            {creating
                                ? <><Icon name="loader" size={13} className="spin" /> Tworzenie…</>
                                : <><Icon name="invoice" size={13} /> {preview.status === 'retry_ready' ? 'Ponów automatyzację' : 'Utwórz i załącz do Allegro'}</>
                            }
                        </button>
                    )}
                    <button className="btn btn-secondary" onClick={onClose}>Zamknij</button>
                </div>
            </div>
        </div>,
        document.body
    )
}

// Mirrors _BREAKDOWN_LOCKED_STATUSES in zdrovena/api/routers/webhooks.py: past
// these the API answers 409, so the table must not offer an edit that cannot land.

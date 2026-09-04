import { useState } from 'react'

import { useAuth } from '../../auth'
import { getShippingDrafts } from '../../api/endpoints'
import { RecipientPhone } from '../shipping/RecipientPhone'
import { apiErrorMessage } from '../shipping/syncSummary'

const NOTE_STYLE = { marginTop: 8, fontSize: '0.88em' }

/**
 * Change the recipient phone of one order, on purpose.
 *
 * The field used to sit on every draft row, where it was one stray click away
 * from rewriting a customer's number. It lives here instead because InPost
 * rejects a shipment with an invalid recipient phone from 2026-09-08, so the
 * capability cannot simply be removed — an order arriving from Shopify with a
 * bad number would become unshippable from the portal. Moving it turns an
 * accident into a deliberate act: you have to know the order number to get here.
 */
export function RecipientPhoneForm() {
    const { getToken } = useAuth()
    const [orderNumber, setOrderNumber] = useState('')
    const [draft, setDraft] = useState(null)
    const [message, setMessage] = useState('')
    const [error, setError] = useState('')
    const [busy, setBusy] = useState(false)

    async function handleFind() {
        const wanted = orderNumber.trim()
        if (!wanted) return
        setBusy(true)
        setDraft(null)
        setMessage('')
        setError('')
        try {
            const token = await getToken()
            const data = await getShippingDrafts({ token })
            const found = (data.drafts || []).find(
                item => String(item.shopify_order_number ?? '') === wanted,
            )
            if (!found) {
                setError(`Nie znaleziono zamówienia ${wanted}`)
                return
            }
            setDraft(found)
        } catch (err) {
            setError(err.message || 'Nie udało się pobrać zamówień')
        } finally {
            setBusy(false)
        }
    }

    async function handleSave(phone) {
        setBusy(true)
        setMessage('')
        setError('')
        try {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ receiver_phone: phone }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
            setDraft({ ...draft, receiver: { ...(draft.receiver || {}), phone } })
            setMessage('Zapisano numer telefonu')
        } catch (err) {
            setError(err.message || 'Nie udało się zapisać telefonu')
        } finally {
            setBusy(false)
        }
    }

    return (
        <div>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <input
                    aria-label="Numer zamówienia"
                    value={orderNumber}
                    onChange={e => setOrderNumber(e.target.value)}
                    placeholder="1731"
                    style={{ width: 120 }} />
                <button type="button" disabled={busy || !orderNumber.trim()} onClick={handleFind}>
                    Znajdź
                </button>
            </div>
            {draft && (
                <div style={{ marginTop: 10 }}>
                    <div className="detail-label">Klient</div>
                    <div>{draft.customer_name || '—'}</div>
                    <div style={{ marginTop: 10 }}>
                        <RecipientPhone
                            phone={draft.receiver?.phone}
                            courier={draft.courier}
                            canEdit
                            saving={busy}
                            onSave={handleSave} />
                    </div>
                </div>
            )}
            {message && <div style={{ ...NOTE_STYLE, color: 'var(--ok, #15803d)' }}>{message}</div>}
            {error && <div style={{ ...NOTE_STYLE, color: 'var(--error)' }}>{error}</div>}
        </div>
    )
}

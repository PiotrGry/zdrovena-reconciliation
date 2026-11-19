// Offer to email the batch label sheet right after it was sent to print (#543).

import { useState } from 'react'
import { createPortal } from 'react-dom'

/** The one place the default recipient of the label email lives. */
export const LABEL_EMAIL_DEFAULT_ADDRESS = 'gryzlo@onet.pl'

/** Turn an API error body into one operator-readable line. FastAPI request
 *  validation (422) returns ``detail`` as a list of issues. */
export function labelEmailErrorMessage(body, response) {
    const detail = body?.detail
    if (Array.isArray(detail)) {
        const first = detail[0]?.msg || ''
        return first.replace(/^Value error,\s*/, '') || `${response.status}`
    }
    return body?.message_pl || detail || `${response.status} ${response.statusText || ''}`.trim()
}

export function LabelEmailModal({ draftIds, getToken, onClose, onSent }) {
    const [address, setAddress] = useState(LABEL_EMAIL_DEFAULT_ADDRESS)
    const [sending, setSending] = useState(false)
    const [error, setError] = useState(null)

    async function send() {
        setSending(true)
        setError(null)
        try {
            const token = await getToken()
            const res = await fetch('/api/shipping/labels/batch/email', {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${token}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ draft_ids: draftIds, to_address: address.trim() }),
            })
            const body = await res.json().catch(() => ({}))
            if (!res.ok) throw new Error(labelEmailErrorMessage(body, res))
            onSent(body)
        } catch (err) {
            setError(err.message)
            setSending(false)
        }
    }

    return createPortal(
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
            <div
                role="dialog"
                aria-label="Wysłać etykiety mailem?"
                data-testid="label-email-modal"
                style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 24, minWidth: 320, display: 'flex', flexDirection: 'column', gap: 16 }}
            >
                <div style={{ fontWeight: 600 }}>Wysłać etykiety mailem?</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <label htmlFor="label-email-address" style={{ fontSize: '0.85em', color: 'var(--text-2)' }}>
                        Adres e-mail
                    </label>
                    <input
                        id="label-email-address"
                        type="email"
                        value={address}
                        onChange={e => setAddress(e.target.value)}
                        disabled={sending}
                        style={{ padding: '6px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', fontSize: '0.9em' }}
                    />
                </div>
                {error && (
                    <div role="alert" style={{ fontSize: '0.85em', color: 'var(--red, #c0392b)' }}>
                        Nie wysłano: {error}
                    </div>
                )}
                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                    <button className="btn btn-ghost" onClick={onClose} disabled={sending}>Nie wysyłaj</button>
                    <button className="btn btn-primary" onClick={send} disabled={sending || !address.trim()}>
                        {sending ? 'Wysyłanie…' : 'Wyślij'}
                    </button>
                </div>
            </div>
        </div>,
        document.body
    )
}

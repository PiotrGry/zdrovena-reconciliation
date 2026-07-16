import { useRef, useState } from 'react'
import { useAuth } from '../../auth'
import { fetchJson } from '../../api'
import { Icon } from '../../components/Icon'
import { useToast } from '../../components/Toast'

const CATEGORY_LABELS = {
    sales: 'Sprzedaż',
    costs: 'Koszty',
    reports: 'Deklaracje',
    bank: 'Bank',
    provider: 'Integracje',
}

const STATUS_LABELS = {
    present: 'Gotowy',
    available_automatically: 'Do pobrania automatycznie',
    missing: 'Brakuje',
    invalid: 'Błąd',
}

export function WorkflowDocuments({ year, month, documents, onUploaded }) {
    const { getToken } = useAuth()
    const { pushToast } = useToast()
    const inputRef = useRef(null)
    const [uploading, setUploading] = useState(false)
    const [dragOver, setDragOver] = useState(false)
    const prefix = `faktury/inbox/${year}-${String(month).padStart(2, '0')}`

    const upload = async files => {
        if (!files.length) return
        setUploading(true)
        try {
            const token = await getToken()
            for (const file of files) {
                await fetchJson(`/api/files/${encodeURIComponent(`${prefix}/${file.name}`)}`, {
                    method: 'PUT',
                    token,
                    headers: { 'Content-Type': file.type || 'application/octet-stream' },
                    body: file,
                })
            }
            pushToast({ kind: 'success', msg: `Wgrano ${files.length} plików dla ${year}-${String(month).padStart(2, '0')}.` })
            await onUploaded?.()
        } catch (error) {
            pushToast({ kind: 'error', msg: `Błąd wgrywania: ${error.message}` })
        } finally {
            setUploading(false)
        }
    }

    const groups = Object.entries(
        (documents ?? []).reduce((result, document) => {
            const category = document.category || 'other'
            result[category] = [...(result[category] ?? []), document]
            return result
        }, {})
    )

    return (
        <section className="card workflow-documents" aria-labelledby="workflow-documents-title">
            <div className="card-head">
                <span className="card-title" id="workflow-documents-title">
                    <Icon name="file" size={14} /> Dokumenty i źródła
                </span>
                <div className="card-head-actions">
                    <input
                        ref={inputRef}
                        type="file"
                        multiple
                        hidden
                        onChange={event => {
                            upload(Array.from(event.target.files ?? []))
                            event.target.value = ''
                        }}
                    />
                    <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        disabled={uploading}
                        onClick={() => inputRef.current?.click()}
                    >
                        <Icon name="upload" size={13} />
                        {uploading ? 'Wgrywam…' : 'Wgraj dokumenty'}
                    </button>
                </div>
            </div>

            <div
                className={`workflow-upload-zone ${dragOver ? 'is-drag' : ''}`}
                onDragOver={event => { event.preventDefault(); setDragOver(true) }}
                onDragLeave={() => setDragOver(false)}
                onDrop={event => {
                    event.preventDefault()
                    setDragOver(false)
                    upload(Array.from(event.dataTransfer.files ?? []))
                }}
            >
                <Icon name="upload" size={15} />
                Pliki trafiają wyłącznie do inboxu okresu <strong>{year}-{String(month).padStart(2, '0')}</strong>.
            </div>

            {groups.length === 0 && (
                <div className="workflow-empty">
                    Uruchom „Kontrolę wstępną”, aby zobaczyć dokumenty i ich źródła.
                </div>
            )}

            {groups.map(([category, items]) => (
                <div className="workflow-document-group" key={category}>
                    <h3>{CATEGORY_LABELS[category] ?? category}</h3>
                    <div className="workflow-document-list">
                        {items.map(document => (
                            <div className={`workflow-document is-${document.status}`} key={document.id}>
                                <span className="workflow-document-icon">
                                    <Icon
                                        name={document.status === 'present' ? 'check' : document.status === 'missing' ? 'alert-circle' : 'cloud'}
                                        size={13}
                                    />
                                </span>
                                <div>
                                    <strong>{document.label}</strong>
                                    <span>{document.message}</span>
                                </div>
                                <div className="workflow-document-source">
                                    <span>{document.source ?? '—'}</span>
                                    <small>{STATUS_LABELS[document.status] ?? document.status}</small>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            ))}
        </section>
    )
}

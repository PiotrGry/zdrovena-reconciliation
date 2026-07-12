import { useState, useRef, useCallback, useEffect } from 'react'
import { useAuth } from '../../auth'
import { useToast } from '../../components/Toast'
import { Icon } from '../../components/Icon'
import { fmtBytes, fmtDate } from '../../data'
import { fetchJson } from '../../api'

const INBOX_PREFIX = 'faktury/inbox'

const REQUIRED_DOCS = [
    { key: 'canva',   label: 'Canva',              hint: 'invoice-XXXXX-YYYYMMDD.pdf',  match: f => /^invoice-\d{5}-\d{8}\.pdf$/i.test(f) },
    { key: 'gads',    label: 'Google Ads',         hint: '0XXXXXXXXX.pdf',              match: f => /^\d{10}\.pdf$/i.test(f) },
    { key: 'pko',     label: 'Wyciąg PKO BP',      hint: 'Wyciag_na_zadanie_*.pdf',     match: f => /^wyciag_na_zadanie_/i.test(f) },
    { key: 'jpk_fa',  label: 'JPK_FA',             hint: 'zdrovena-...-jpk_fa.xml',     match: f => /jpk.?fa/i.test(f),  link: 'https://zdrovena.fakturownia.pl/reports/jpk_fa?kind=jpk_fa&query_date_kind=transaction_date&form_variant=4' },
    { key: 'jpk_v7m', label: 'JPK_V7M',            hint: 'zdrovena-...-jpkv7m.xml',     match: f => /jpk.?v7/i.test(f),  link: 'https://zdrovena.fakturownia.pl/accounting/app/reports/jpk_vat' },
    { key: 'vat',     label: 'Wykaz sprzedaży VAT', hint: 'zdrovena-YYYY-MM-DD_*.pdf',  match: f => /^zdrovena-\d{4}-\d{2}-\d{2}_/i.test(f), link: 'https://zdrovena.fakturownia.pl/reports/income_tax_records' },
]

function extOf(name) { const m = name?.match(/\.([^.]+)$/); return m ? m[1].toLowerCase() : '' }
function extChipClass(ext) {
    if (ext === 'pdf') return 'ext-chip pdf'
    if (ext === 'xml') return 'ext-chip xml'
    if (['zip','tar','gz'].includes(ext)) return 'ext-chip zip'
    return 'ext-chip'
}

/**
 * Lista 6 wymaganych dokumentów z ikonkami sukces/brak. Pod listą collapsible
 * panel pełnej zawartości inboxu (drag-drop, usuwanie). Wywołuje onStatusChange
 * z true/false gdy wszystkie wymagane są obecne.
 */
export function DocChecklist({ onStatusChange }) {
    const { getToken } = useAuth()
    const { pushToast } = useToast()
    const [items, setItems] = useState([])
    const [loading, setLoading] = useState(true)
    const [dragOver, setDragOver] = useState(false)
    const [showAll, setShowAll] = useState(false)
    const fileInput = useRef(null)

    const load = useCallback(async () => {
        setLoading(true)
        try {
            const token = await getToken()
            const data = await fetchJson(
                `/api/files?prefix=${encodeURIComponent(INBOX_PREFIX)}`,
                { token },
            )
            setItems((data.items ?? data).filter(i =>
                !(i.is_directory || (i.key ?? i.name ?? '').endsWith('/'))
            ))
        } catch (e) {
            setItems([])
            pushToast({ kind: 'error', msg: `Nie udało się wczytać listy dokumentów: ${e.message}` })
        } finally {
            setLoading(false)
        }
    }, [getToken, pushToast])

    useEffect(() => { load() }, [load])

    const getKey = i => i.key || i.name || ''
    const getName = i => i.name || getKey(i).split('/').pop() || ''

    const upload = useCallback(async (file) => {
        try {
            const token = await getToken()
            await fetchJson(`/api/files/${encodeURIComponent(`${INBOX_PREFIX}/${file.name}`)}`, {
                method: 'PUT',
                token,
                headers: { 'Content-Type': file.type || 'application/octet-stream' },
                body: file,
            })
            load()
        } catch (e) {
            pushToast({ kind: 'error', msg: `Błąd wgrywania ${file.name}: ${e.message}` })
        }
    }, [getToken, load, pushToast])

    const deleteFile = useCallback(async (key) => {
        if (!window.confirm(`Usuń "${key.split('/').pop()}" z inbox?`)) return
        try {
            const token = await getToken()
            await fetchJson(`/api/files/${encodeURIComponent(key)}`, {
                method: 'DELETE',
                token,
            })
            load()
        } catch (e) {
            pushToast({ kind: 'error', msg: `Błąd usuwania: ${e.message}` })
        }
    }, [getToken, load, pushToast])

    const names = items.map(getName)
    const matched = REQUIRED_DOCS.map(doc => ({
        ...doc,
        found: names.find(f => doc.match(f)) ?? null,
    }))
    const allFound = matched.every(d => d.found)
    const missingCount = matched.filter(d => !d.found).length

    useEffect(() => {
        // null = still loading (unknown), true/false = resolved
        onStatusChange?.(loading ? null : allFound)
    }, [allFound, loading, onStatusChange])

    return (
        <section className="card doc-checklist" aria-labelledby="doc-checklist-title">
            <div className="card-head">
                <span className="card-title" id="doc-checklist-title">
                    <Icon name={allFound ? 'check' : 'alert-circle'} size={14} />
                    Wymagane dokumenty
                    {!loading && (
                        <span className="card-sub">
                            {allFound ? 'wszystkie obecne' : `brakuje ${missingCount} z ${matched.length}`}
                        </span>
                    )}
                </span>
                <div className="card-head-actions">
                    <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={load}
                        disabled={loading}
                        title="Odśwież listę plików"
                    >
                        <Icon name="refresh-cw" size={12} className={loading ? 'spinning' : ''} />
                        Odśwież
                    </button>
                    <input
                        ref={fileInput}
                        type="file"
                        multiple
                        style={{ display: 'none' }}
                        onChange={e => {
                            Array.from(e.target.files).forEach(upload)
                            e.target.value = ''
                        }}
                    />
                    <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => fileInput.current.click()}
                    >
                        <Icon name="upload" size={13} /> Wgraj
                    </button>
                </div>
            </div>

            <ul className="doc-list" aria-label="Lista wymaganych dokumentów">
                {matched.map(doc => (
                    <li key={doc.key} className={`doc-item ${doc.found ? 'is-ok' : 'is-missing'}`}>
                        <span className="doc-icon" aria-hidden="true">
                            <Icon name={doc.found ? 'check' : 'alert-circle'} size={14} />
                        </span>
                        <span className="doc-label">
                            <strong>{doc.label}</strong>
                            <span className="doc-hint">{doc.found ?? doc.hint}</span>
                        </span>
                        {!doc.found && doc.link && (
                            <a
                                href={doc.link}
                                target="_blank"
                                rel="noreferrer"
                                className="btn btn-ghost btn-sm"
                            >
                                <Icon name="external-link" size={12} /> Pobierz z Fakturownia
                            </a>
                        )}
                        {!doc.found && !doc.link && (
                            <button
                                type="button"
                                className="btn btn-ghost btn-sm"
                                onClick={() => fileInput.current.click()}
                            >
                                <Icon name="upload" size={12} /> Wgraj
                            </button>
                        )}
                    </li>
                ))}
            </ul>

            <details
                className="doc-files"
                open={showAll}
                onToggle={e => setShowAll(e.target.open)}
            >
                <summary>
                    <Icon name="caret" size={11} />
                    {showAll ? 'Ukryj' : 'Pokaż'} wszystkie pliki w inbox{' '}
                    <span className="doc-files-count">({items.length})</span>
                </summary>
                <div
                    className={`doc-files-body ${dragOver ? 'is-drag' : ''}`}
                    onDragOver={e => { e.preventDefault(); setDragOver(true) }}
                    onDragLeave={() => setDragOver(false)}
                    onDrop={e => {
                        e.preventDefault()
                        setDragOver(false)
                        Array.from(e.dataTransfer.files).forEach(upload)
                    }}
                >
                    {loading && <div className="doc-files-empty">Ładowanie…</div>}
                    {!loading && items.length === 0 && (
                        <div className={`dropzone${dragOver ? ' active' : ''}`}>
                            <span className="hint">
                                {dragOver ? 'Upuść pliki tutaj' : 'Przeciągnij pliki lub kliknij „Wgraj"'}
                            </span>
                        </div>
                    )}
                    {!loading && items.length > 0 && (
                        <table className="files">
                            <tbody>
                                {items.map(file => {
                                    const n = getName(file)
                                    const ext = extOf(n)
                                    return (
                                        <tr key={getKey(file)}>
                                            <td>
                                                <div className="name-cell">
                                                    <span className={extChipClass(ext)}>
                                                        {ext.toUpperCase() || '—'}
                                                    </span>
                                                    <span className="main-text">{n}</span>
                                                </div>
                                            </td>
                                            <td className="mono">{fmtBytes(file.size)}</td>
                                            <td className="mono dim">{fmtDate(file.last_modified)}</td>
                                            <td>
                                                <div className="row-actions">
                                                    <button
                                                        type="button"
                                                        className="icon-btn danger"
                                                        title="Usuń"
                                                        aria-label={`Usuń plik ${n}`}
                                                        onClick={() => deleteFile(getKey(file))}
                                                    >
                                                        <Icon name="trash" size={15} />
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>
                                    )
                                })}
                            </tbody>
                        </table>
                    )}
                </div>
            </details>
        </section>
    )
}

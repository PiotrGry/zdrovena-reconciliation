import { createContext, useCallback, useContext, useRef, useState } from 'react'

const ToastCtx = createContext(null)

// Globalny system toastów. Błędy są domyślnie "sticky" (nie znikają po 3 s),
// żeby operator zdążył je przeczytać — pozostałe warianty auto-znikają.
export function ToastProvider({ children }) {
    const [toasts, setToasts] = useState([])
    const idRef = useRef(0)

    const removeToast = useCallback((id) => {
        setToasts((list) => list.filter((t) => t.id !== id))
    }, [])

    const pushToast = useCallback(
        ({ kind = 'info', msg, sticky }) => {
            const id = ++idRef.current
            const isSticky = sticky ?? kind === 'error'
            setToasts((list) => [...list, { id, kind, msg }])
            if (!isSticky) {
                setTimeout(() => removeToast(id), 3000)
            }
            return id
        },
        [removeToast],
    )

    return (
        <ToastCtx.Provider value={{ pushToast, removeToast }}>
            {children}
            <div className="toast-wrap" aria-live="polite">
                {toasts.map((t) => (
                    <div
                        key={t.id}
                        className={`toast toast-${t.kind}`}
                        role={t.kind === 'error' ? 'alert' : 'status'}
                    >
                        <span className="dot" aria-hidden="true" />
                        <span className="toast-msg">{t.msg}</span>
                        <button
                            type="button"
                            className="toast-close"
                            aria-label="Zamknij"
                            onClick={() => removeToast(t.id)}
                        >
                            ×
                        </button>
                    </div>
                ))}
            </div>
        </ToastCtx.Provider>
    )
}

export function useToast() {
    const ctx = useContext(ToastCtx)
    if (!ctx) {
        throw new Error('useToast musi być użyty wewnątrz <ToastProvider>')
    }
    return ctx
}

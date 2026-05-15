import { useState, useRef, useEffect } from 'react'
import { useAuth } from '../auth'
import { useT } from '../lang'
import { BrandWave, Icon } from './Icon'
import { MONTHS_PL } from '../data'

const NOW = new Date()
const PERIOD = `${MONTHS_PL[NOW.getMonth()]} ${NOW.getFullYear()}`

export function Header() {
    const { account, roles, logout } = useAuth()
    const { t, lang, setLang } = useT()
    const [open, setOpen] = useState(false)
    const userRef = useRef(null)

    useEffect(() => {
        if (!open) return
        const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
        const onClick = (e) => {
            if (userRef.current && !userRef.current.contains(e.target)) setOpen(false)
        }
        document.addEventListener('keydown', onKey)
        document.addEventListener('mousedown', onClick)
        return () => {
            document.removeEventListener('keydown', onKey)
            document.removeEventListener('mousedown', onClick)
        }
    }, [open])

    const name = account?.name || account?.username || 'User'
    const initials = name.split(' ').map(p => p[0]).join('').slice(0, 2).toUpperCase()
    const roleKey = roles.includes('admin') ? 'role_admin'
        : roles.includes('accountant') ? 'role_accountant'
            : 'role_viewer'

    return (
        <header className="header">
            <div className="brand">
                <BrandWave />
                <span className="brand-name">Zdrovena</span>
                <div className="brand-divider" />
                <span className="brand-sub">{t[lang].subtitle}</span>
            </div>

            <div className="header-right">
                <span className="period-chip" aria-label={`${t[lang].period}: ${PERIOD}`}>
                    <Icon name="caret" size={14} />
                    {t[lang].period}: <strong>{PERIOD}</strong>
                </span>

                <div className="seg-tiny" role="group" aria-label="Język">
                    {['pl', 'en'].map(l => (
                        <button
                            key={l}
                            className={`tiny-btn${lang === l ? ' active' : ''}`}
                            onClick={() => setLang(l)}
                            aria-pressed={lang === l}
                        >
                            {l.toUpperCase()}
                        </button>
                    ))}
                </div>

                <button className="btn-ghost icon-btn" aria-label="Powiadomienia">
                    <Icon name="bell" size={18} />
                </button>

                <div className="user" ref={userRef}>
                    <button
                        type="button"
                        className="user-trigger"
                        onClick={() => setOpen(o => !o)}
                        aria-haspopup="menu"
                        aria-expanded={open}
                        aria-label={`${name} — ${t[lang][roleKey]}`}
                    >
                        <div className="avatar">{initials}</div>
                        <div className="user-meta">
                            <div className="name">{name.split(' ')[0]}</div>
                            <div className="role">{t[lang][roleKey]}</div>
                        </div>
                        <Icon name={open ? 'caretUp' : 'caret'} size={14} />
                    </button>
                    {open && (
                        <div className="user-dropdown" role="menu">
                            <button
                                type="button"
                                role="menuitem"
                                onClick={() => { setOpen(false); logout() }}
                                className="danger"
                            >
                                <Icon name="logout" size={14} />
                                {t[lang].logout}
                            </button>
                        </div>
                    )}
                </div>
            </div>
        </header>
    )
}

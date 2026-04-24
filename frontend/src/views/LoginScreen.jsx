import { useAuth } from '../auth'
import { useT } from '../lang'
import { BrandWave } from '../components/Icon'

export default function LoginScreen() {
    const { login } = useAuth()
    const { t, lang } = useT()
    const T = t[lang]

    return (
        <div className="login-screen">
            <div className="login-card">
                <div className="login-logo">
                    <BrandWave className="brand-wave" style={{ width: 32, height: 32 }} />
                    <span className="brand-name" style={{ fontSize: 15 }}>Zdrovena</span>
                </div>
                <h1 className="login-title">{T.login_title}</h1>
                <p className="login-sub">{T.login_sub}</p>
                <button className="login-btn" onClick={login}>
                    <svg width="18" height="18" viewBox="0 0 21 21" fill="none" aria-hidden="true">
                        <rect x="1" y="1" width="9" height="9" fill="#F25022" />
                        <rect x="11" y="1" width="9" height="9" fill="#7FBA00" />
                        <rect x="1" y="11" width="9" height="9" fill="#00A4EF" />
                        <rect x="11" y="11" width="9" height="9" fill="#FFB900" />
                    </svg>
                    {T.login_btn}
                </button>
                <p className="login-hint">{T.login_hint}</p>
            </div>
        </div>
    )
}

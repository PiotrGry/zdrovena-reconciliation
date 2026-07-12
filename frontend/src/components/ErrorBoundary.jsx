import { Component } from 'react'

// Globalna bariera błędów. Łapie wyjątki renderowania Reacta, żeby crash
// jednego widoku nie wygaszał całej aplikacji (biały ekran) — nagłówek
// i nawigacja pozostają używalne. Musi być klasą: hooki nie obsługują
// componentDidCatch.
export class ErrorBoundary extends Component {
    constructor(props) {
        super(props)
        this.state = { error: null }
    }

    static getDerivedStateFromError(error) {
        return { error }
    }

    componentDidCatch(error, info) {
        console.error('Nieobsłużony błąd widoku:', error, info)
    }

    componentDidUpdate(prevProps) {
        // Reset bariery po zmianie widoku (np. przejściu w menu), żeby
        // operator mógł opuścić zepsuty ekran bez przeładowania strony.
        if (prevProps.resetKey !== this.props.resetKey && this.state.error) {
            this.setState({ error: null })
        }
    }

    render() {
        if (this.state.error) {
            return (
                <div className="error-boundary" role="alert">
                    <div className="error-boundary-box">
                        <h2>Coś poszło nie tak</h2>
                        <p>
                            Ten widok napotkał nieoczekiwany błąd. Spróbuj przejść do
                            innej sekcji lub odświeżyć stronę.
                        </p>
                        {this.state.error?.message && (
                            <pre className="error-boundary-detail">
                                {this.state.error.message}
                            </pre>
                        )}
                        <button
                            type="button"
                            className="btn btn-primary"
                            onClick={() => window.location.reload()}
                        >
                            Odśwież stronę
                        </button>
                    </div>
                </div>
            )
        }
        return this.props.children
    }
}

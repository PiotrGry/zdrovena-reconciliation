import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PeriodStatus } from './PeriodStatus'

const inspection = {
    year: 2026,
    month: 8,
    computed_at: '2026-09-02T08:15:00+00:00',
    metrics: { ready: false },
    documents: [
        { id: 'sales', category: 'sales', label: 'Sprzedaż', status: 'ok', required: true },
        { id: 'canva', category: 'vendor', label: 'Canva', status: 'missing', required: true },
    ],
    issues: [
        { id: 'canva-missing', severity: 'blocker', message: 'Brak faktury Canva.', stage: 'check' },
    ],
    run: null,
}

describe('PeriodStatus', () => {
    it('says what is missing without anyone starting a close', () => {
        render(<PeriodStatus inspection={inspection} />)

        expect(screen.getByText('Brak faktury Canva.')).toBeInTheDocument()
        expect(screen.getByText('Canva')).toBeInTheDocument()
    })

    it('shows when the answer was computed, so nobody reads a stale one', () => {
        render(<PeriodStatus inspection={inspection} />)

        expect(screen.getByTestId('period-status-computed-at')).toBeInTheDocument()
    })

    it('offers no button that starts anything', () => {
        // This panel is where you look, not where you act. A button here would
        // put the operator back into the state machine they came to avoid.
        render(<PeriodStatus inspection={inspection} />)

        expect(screen.queryAllByRole('button')).toHaveLength(0)
    })

    it('states plainly when the period is complete', () => {
        render(
            <PeriodStatus
                inspection={{ ...inspection, metrics: { ready: true }, issues: [] }} />,
        )

        expect(screen.getByText('Komplet — okres gotowy do zamknięcia')).toBeInTheDocument()
    })

    it('says that nothing has been started yet when there is no run', () => {
        render(<PeriodStatus inspection={inspection} />)

        expect(screen.getByText('Zamykanie nierozpoczęte')).toBeInTheDocument()
    })

    it('reports the run status when one exists', () => {
        render(
            <PeriodStatus inspection={{ ...inspection, run: { status: 'needs_input' } }} />,
        )

        expect(screen.queryByText('Zamykanie nierozpoczęte')).not.toBeInTheDocument()
        expect(screen.getByText(/needs_input/)).toBeInTheDocument()
    })

    it('renders nothing before the first answer arrives', () => {
        const { container } = render(<PeriodStatus inspection={null} />)

        expect(container).toBeEmptyDOMElement()
    })
})

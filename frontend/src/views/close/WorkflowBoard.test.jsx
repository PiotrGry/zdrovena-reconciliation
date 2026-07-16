import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { WorkflowBoard } from './WorkflowBoard'

function run(overrides = {}) {
    const steps = Object.fromEntries(
        ['check', 'sales', 'costs', 'reports', 'bank', 'package', 'send']
            .map(id => [id, { status: 'pending', message: null }])
    )
    return {
        run_id: '12345678-aaaa-bbbb-cccc-123456789012',
        active_action: null,
        steps,
        issues: [],
        metrics: {},
        artifacts: [],
        ...overrides,
    }
}

describe('WorkflowBoard', () => {
    it('requires preflight before collection actions', () => {
        render(<WorkflowBoard run={run()} onAction={() => {}} />)

        expect(screen.getByRole('button', { name: /Sprawdź ponownie/ })).toBeEnabled()
        expect(screen.getByRole('button', { name: /Pobierz sprzedaż/ })).toBeDisabled()
        expect(screen.getByRole('button', { name: /Zbuduj paczkę/ })).toBeDisabled()
    })

    it('keeps package and send as explicit manual gates', async () => {
        const onAction = vi.fn()
        const ready = run()
        ready.steps = Object.fromEntries(
            Object.entries(ready.steps).map(([id, step]) => [
                id,
                ['check', 'sales', 'costs', 'reports', 'bank'].includes(id)
                    ? { ...step, status: 'done' }
                    : step,
            ])
        )
        render(<WorkflowBoard run={ready} onAction={onAction} />)

        const packageButton = screen.getByRole('button', { name: /Zbuduj paczkę/ })
        expect(packageButton).toBeEnabled()
        expect(screen.getByRole('button', { name: /Wyślij paczkę/ })).toBeDisabled()

        await userEvent.click(packageButton)
        expect(onAction).toHaveBeenCalledWith('package')
    })

    it('shows the selected original source for PulsePure', () => {
        const ready = run({
            metrics: {
                cost_found_vendors: {
                    PulsePure: 'Fakturownia — oryginalny załącznik',
                },
            },
        })
        ready.steps.costs.status = 'done'

        render(<WorkflowBoard run={ready} onAction={() => {}} />)

        expect(
            screen.getByText('PulsePure: Fakturownia — oryginalny załącznik')
        ).toBeInTheDocument()
    })
})

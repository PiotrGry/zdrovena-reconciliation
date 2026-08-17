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

    it('lets a failed stage be waived and stops it gating the package', async () => {
        const onWaive = vi.fn()
        const blocked = run()
        blocked.steps.check.status = 'done'
        blocked.steps.sales.status = 'done'
        blocked.steps.reports.status = 'done'
        blocked.steps.bank.status = 'done'
        blocked.steps.costs.status = 'failed'
        render(<WorkflowBoard run={blocked} onAction={() => {}} onWaive={onWaive} />)

        expect(screen.getByRole('button', { name: /Zbuduj paczkę/ })).toBeDisabled()
        await userEvent.click(screen.getByRole('button', { name: /Pomiń mimo problemów/ }))
        expect(onWaive).toHaveBeenCalledWith('step:costs')

        blocked.steps.costs.waived = true
        render(<WorkflowBoard run={blocked} onAction={() => {}} onWaive={onWaive} />)
        const packageButtons = screen.getAllByRole('button', { name: /Zbuduj paczkę/ })
        expect(packageButtons[packageButtons.length - 1]).toBeEnabled()
    })

    it('keeps waived issues from blocking the package', () => {
        const ready = run()
        ready.steps = Object.fromEntries(
            Object.entries(ready.steps).map(([id, step]) => [
                id,
                ['check', 'sales', 'costs', 'reports', 'bank'].includes(id)
                    ? { ...step, status: 'done' }
                    : step,
            ])
        )
        ready.issues = [{ id: 'brak', severity: 'blocker', message: 'Brak', stage: 'check' }]

        const { rerender } = render(<WorkflowBoard run={ready} onAction={() => {}} />)
        expect(screen.getByRole('button', { name: /Zbuduj paczkę/ })).toBeDisabled()

        rerender(
            <WorkflowBoard
                run={{ ...ready, issues: [{ ...ready.issues[0], waived: true }] }}
                onAction={() => {}}
            />
        )
        expect(screen.getByRole('button', { name: /Zbuduj paczkę/ })).toBeEnabled()
    })

    it('offers no skip for stages that are not waivable', () => {
        const failed = run()
        failed.steps.package.status = 'failed'
        failed.steps.send.status = 'failed'
        render(<WorkflowBoard run={failed} onAction={() => {}} onWaive={() => {}} />)

        expect(screen.queryByRole('button', { name: /Pomiń mimo problemów/ })).toBeNull()
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

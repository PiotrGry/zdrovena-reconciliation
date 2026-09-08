import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { PackagesEditor } from './PackagesEditor'

function setup({ canEdit = true, breakdown = [{ type: '1-pak', qty: 1 }], onSave = vi.fn() } = {}) {
    render(<PackagesEditor breakdown={breakdown} canEdit={canEdit} onSave={onSave} />)
    return { onSave }
}

describe('PackagesEditor', () => {
    it('sends the edited plan on save', async () => {
        const user = userEvent.setup()
        const { onSave } = setup()

        await user.selectOptions(screen.getByLabelText('Typ paczki 1'), 'szkło')
        await user.clear(screen.getByLabelText('Liczba sztuk 1'))
        await user.type(screen.getByLabelText('Liczba sztuk 1'), '3')
        await user.click(screen.getByRole('button', { name: 'Zapisz paczki' }))

        await waitFor(() => expect(onSave).toHaveBeenCalledWith([{ type: 'szkło', qty: 3 }]))
    })

    it('adds and removes rows', async () => {
        const user = userEvent.setup()
        const { onSave } = setup()

        await user.click(screen.getByRole('button', { name: 'Dodaj typ paczki' }))
        await user.selectOptions(screen.getByLabelText('Typ paczki 2'), 'szkło')
        await user.click(screen.getByRole('button', { name: 'Usuń typ paczki 1' }))
        await user.click(screen.getByRole('button', { name: 'Zapisz paczki' }))

        await waitFor(() => expect(onSave).toHaveBeenCalledWith([{ type: 'szkło', qty: 1 }]))
    })

    it('does not offer the suspended glass 2-pak', () => {
        setup()

        const options = [...screen.getByLabelText('Typ paczki 1').options].map(o => o.value)
        expect(options).toEqual(['3-pak', '2-pak', '1-pak', 'pół-pak', 'szkło'])
    })

    it('keeps a stored suspended type selectable and counts it as two parcels', () => {
        // Drafts created before szkło-2pak was suspended still carry it. Hiding
        // the type would blank the row; counting it as one parcel would promise
        // one label where the backend books two.
        setup({ breakdown: [{ type: 'szkło-2pak', qty: 1 }] })

        expect(screen.getByLabelText('Typ paczki 1')).toHaveValue('szkło-2pak')
        expect(screen.getByText(/Razem 2 paczki/)).toBeInTheDocument()
    })

    it('will not let the operator save an empty plan', async () => {
        const user = userEvent.setup()
        const { onSave } = setup()

        await user.click(screen.getByRole('button', { name: 'Usuń typ paczki 1' }))

        expect(screen.getByRole('button', { name: 'Zapisz paczki' })).toBeDisabled()
        expect(onSave).not.toHaveBeenCalled()
    })

    it('keeps an in-progress edit when the poll returns the same plan', async () => {
        // ShippingView refetches every 5s and hands back a new array each time.
        // Resetting on array identity would wipe the operator's edit mid-typing.
        const user = userEvent.setup()
        const { rerender } = render(
            <PackagesEditor breakdown={[{ type: '1-pak', qty: 1 }]} canEdit onSave={vi.fn()} />,
        )

        await user.selectOptions(screen.getByLabelText('Typ paczki 1'), 'szkło')
        rerender(
            <PackagesEditor breakdown={[{ type: '1-pak', qty: 1 }]} canEdit onSave={vi.fn()} />,
        )

        expect(screen.getByLabelText('Typ paczki 1')).toHaveValue('szkło')
    })

    it('renders read-only once the draft can no longer be edited', () => {
        setup({ canEdit: false, breakdown: [{ type: '2-pak', qty: 2 }] })

        expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
        expect(screen.getByText('2-pak')).toBeInTheDocument()
        expect(screen.getByText('2')).toBeInTheDocument()
    })

    it('shows the total parcel count so the operator sees how many labels this makes', () => {
        setup({ breakdown: [{ type: '1-pak', qty: 2 }, { type: 'szkło', qty: 1 }] })

        expect(screen.getByText('Razem 3 paczki — tyle etykiet i numerów śledzenia')).toBeInTheDocument()
    })
})

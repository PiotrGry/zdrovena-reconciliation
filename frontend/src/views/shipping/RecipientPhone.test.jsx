import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { RecipientPhone } from './RecipientPhone'

describe('RecipientPhone', () => {
    it('saves the number the operator typed', async () => {
        const user = userEvent.setup()
        const onSave = vi.fn()
        render(<RecipientPhone phone={null} canEdit onSave={onSave} />)

        await user.type(screen.getByLabelText('Telefon odbiorcy'), '600100200')
        await user.click(screen.getByRole('button', { name: 'Zapisz telefon' }))

        await waitFor(() => expect(onSave).toHaveBeenCalledWith('600100200'))
    })

    it('will not save an unchanged number', () => {
        render(<RecipientPhone phone="+48600100200" canEdit onSave={vi.fn()} />)

        expect(screen.getByRole('button', { name: 'Zapisz telefon' })).toBeDisabled()
    })

    it('will not save an empty field', async () => {
        const user = userEvent.setup()
        render(<RecipientPhone phone="+48600100200" canEdit onSave={vi.fn()} />)

        await user.clear(screen.getByLabelText('Telefon odbiorcy'))

        expect(screen.getByRole('button', { name: 'Zapisz telefon' })).toBeDisabled()
    })

    it('warns when InPost would reject the stored number', () => {
        render(<RecipientPhone phone={null} canEdit onSave={vi.fn()} courier="inpost" />)

        expect(screen.getByText('InPost wymaga telefonu odbiorcy')).toBeInTheDocument()
    })

    it('does not warn for other carriers', () => {
        render(<RecipientPhone phone={null} canEdit onSave={vi.fn()} courier="apaczka" />)

        expect(screen.queryByText('InPost wymaga telefonu odbiorcy')).not.toBeInTheDocument()
    })

    it('does not warn once the stored number is usable', () => {
        render(<RecipientPhone phone="+48600100200" canEdit onSave={vi.fn()} courier="inpost" />)

        expect(screen.queryByText('InPost wymaga telefonu odbiorcy')).not.toBeInTheDocument()
    })

    it('keeps an in-progress edit when the poll returns the same number', async () => {
        // ShippingView refetches every 5s and re-renders with the same value.
        const user = userEvent.setup()
        const { rerender } = render(
            <RecipientPhone phone="+48600100200" canEdit onSave={vi.fn()} />,
        )

        await user.clear(screen.getByLabelText('Telefon odbiorcy'))
        await user.type(screen.getByLabelText('Telefon odbiorcy'), '500600700')
        rerender(<RecipientPhone phone="+48600100200" canEdit onSave={vi.fn()} />)

        expect(screen.getByLabelText('Telefon odbiorcy')).toHaveValue('500600700')
    })

    it('renders read-only without permission', () => {
        render(<RecipientPhone phone="+48600100200" canEdit={false} onSave={vi.fn()} />)

        expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
        expect(screen.getByText('+48600100200')).toBeInTheDocument()
    })
})

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { RecipientPhoneForm } from './RecipientPhoneForm'
import { jsonResponse, mockFetch } from '../../test/http'
import { renderWithProviders } from '../../test/render'

const draft = {
    id: 'draft-1731',
    shopify_order_number: '1731',
    customer_name: 'Magiczna Zielarnia',
    courier: 'inpost',
    status: 'pending',
    receiver: { phone: '+48731973804' },
}

describe('RecipientPhoneForm', () => {
    it('finds an order and shows whose number it is about to change', async () => {
        const user = userEvent.setup()
        mockFetch(url => {
            if (url === '/api/shipping/drafts') return jsonResponse({ drafts: [draft] })
            throw new Error(`Unexpected request: ${url}`)
        })
        renderWithProviders(<RecipientPhoneForm />)

        await user.type(screen.getByLabelText('Numer zamówienia'), '1731')
        await user.click(screen.getByRole('button', { name: 'Znajdź' }))

        expect(await screen.findByText('Magiczna Zielarnia')).toBeInTheDocument()
        expect(screen.getByLabelText('Telefon odbiorcy')).toHaveValue('+48731973804')
    })

    it('says when no order carries that number', async () => {
        const user = userEvent.setup()
        mockFetch(url => {
            if (url === '/api/shipping/drafts') return jsonResponse({ drafts: [draft] })
            throw new Error(`Unexpected request: ${url}`)
        })
        renderWithProviders(<RecipientPhoneForm />)

        await user.type(screen.getByLabelText('Numer zamówienia'), '9999')
        await user.click(screen.getByRole('button', { name: 'Znajdź' }))

        expect(await screen.findByText('Nie znaleziono zamówienia 9999')).toBeInTheDocument()
    })

    it('saves the new number against the order it found', async () => {
        const user = userEvent.setup()
        const patches = []
        mockFetch((url, init = {}) => {
            if (url === '/api/shipping/drafts') return jsonResponse({ drafts: [draft] })
            if (url === '/api/shipping/drafts/draft-1731' && init.method === 'PATCH') {
                patches.push(JSON.parse(init.body))
                return jsonResponse({})
            }
            throw new Error(`Unexpected request: ${init.method || 'GET'} ${url}`)
        })
        renderWithProviders(<RecipientPhoneForm />)

        await user.type(screen.getByLabelText('Numer zamówienia'), '1731')
        await user.click(screen.getByRole('button', { name: 'Znajdź' }))
        const field = await screen.findByLabelText('Telefon odbiorcy')
        await user.clear(field)
        await user.type(field, '600100200')
        await user.click(screen.getByRole('button', { name: 'Zapisz telefon' }))

        await waitFor(() => expect(patches).toEqual([{ receiver_phone: '600100200' }]))
        expect(await screen.findByText('Zapisano numer telefonu')).toBeInTheDocument()
    })

    it('reports a rejected number instead of pretending it saved', async () => {
        const user = userEvent.setup()
        mockFetch((url, init = {}) => {
            if (url === '/api/shipping/drafts') return jsonResponse({ drafts: [draft] })
            if (init.method === 'PATCH') {
                return jsonResponse({ detail: 'Numer telefonu nie jest poprawny' }, { status: 400 })
            }
            throw new Error(`Unexpected request: ${init.method || 'GET'} ${url}`)
        })
        renderWithProviders(<RecipientPhoneForm />)

        await user.type(screen.getByLabelText('Numer zamówienia'), '1731')
        await user.click(screen.getByRole('button', { name: 'Znajdź' }))
        const field = await screen.findByLabelText('Telefon odbiorcy')
        await user.clear(field)
        await user.type(field, '123')
        await user.click(screen.getByRole('button', { name: 'Zapisz telefon' }))

        expect(await screen.findByText(/nie jest poprawny/)).toBeInTheDocument()
    })
})

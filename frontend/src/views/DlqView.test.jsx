import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import DlqView from './DlqView'
import { emptyResponse, jsonResponse, mockFetch } from '../test/http'
import { renderWithProviders } from '../test/render'

const entry = {
    id: 'dlq-1',
    created_at: '2026-07-15T10:00:00Z',
    source: 'shopify',
    payload: { order_number: '#1576', customer: { email: 'buyer@example.com' } },
    retries: 2,
    last_error: 'Invalid shipping address',
}

function installDlqFetch({ entries = [entry], retryStatus = 200 } = {}) {
    let listCalls = 0
    const fetchMock = mockFetch((url, init = {}) => {
        if (url === '/api/shipping/drafts/dlq') {
            listCalls += 1
            return jsonResponse({ entries: listCalls > 1 ? [] : entries })
        }
        if (url === '/api/shipping/drafts/dlq/dlq-1/retry' && init.method === 'POST') {
            if (retryStatus >= 400) {
                return jsonResponse({ message_pl: 'Adres nadal jest niepoprawny.', correlation_id: 'corr-dlq' }, { status: retryStatus })
            }
            return jsonResponse({ status: 'retried' })
        }
        if (url === '/api/shipping/drafts/dlq/dlq-1' && init.method === 'DELETE') {
            return emptyResponse()
        }
        throw new Error(`Unexpected request: ${init.method || 'GET'} ${url}`)
    })
    return { fetchMock }
}

describe('DlqView', () => {
    it('displays failed events and expands payload details', async () => {
        installDlqFetch()

        renderWithProviders(<DlqView />)

        expect(await screen.findByText('#1576')).toBeInTheDocument()
        expect(screen.getByText('Invalid shipping address')).toBeInTheDocument()

        await userEvent.click(screen.getByRole('button', { name: /Payload/ }))

        expect(screen.getByText(/buyer@example.com/)).toBeInTheDocument()
    })

    it('retries an event and reflects success', async () => {
        installDlqFetch()

        renderWithProviders(<DlqView />)
        await screen.findByText('#1576')

        await userEvent.click(screen.getByRole('button', { name: /Ponów/ }))

        expect(await screen.findByText('Wpis DLQ ponowiony — draft utworzony.')).toBeInTheDocument()
        expect(screen.getByText('Kolejka DLQ jest pusta — brak nieudanych draftów.')).toBeInTheDocument()
    })

    it('shows a safe Polish message when retry fails', async () => {
        installDlqFetch({ retryStatus: 422 })

        renderWithProviders(<DlqView />)
        await screen.findByText('#1576')

        const row = screen.getByText('#1576').closest('tr')
        await userEvent.click(within(row).getByRole('button', { name: /Ponów/ }))

        expect(await screen.findByText(/Ponowienie nie powiodło się: Adres nadal jest niepoprawny/)).toBeInTheDocument()
        expect(screen.getByText(/corr-dlq/)).toBeInTheDocument()
    })

    it('discards an event after confirmation', async () => {
        installDlqFetch()
        vi.spyOn(window, 'confirm').mockReturnValue(true)

        renderWithProviders(<DlqView />)
        await screen.findByText('#1576')

        await userEvent.click(screen.getByRole('button', { name: /Usuń/ }))

        expect(await screen.findByText('Wpis DLQ usunięty.')).toBeInTheDocument()
        expect(screen.getByText('Kolejka DLQ jest pusta — brak nieudanych draftów.')).toBeInTheDocument()
    })
})

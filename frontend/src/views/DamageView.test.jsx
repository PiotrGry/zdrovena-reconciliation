import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import DamageView from './DamageView'
import { jsonResponse, mockFetch } from '../test/http'
import { renderWithProviders } from '../test/render'

const reviewCase = {
    id: 'damage-1',
    detected_at: '2026-07-15T13:40:42Z',
    status: 'needs_review',
    classification: 'damage',
    confidence: 'high',
    tracking_number: 'A0052HFZF6',
    order_number: '1648',
    customer_name: 'Jan Kowalski',
    sources: ['allegro_tracking'],
    evidence: [{ code: 'ISSUE', description: 'Parcel has been damaged' }],
}

describe('DamageView', () => {
    it('shows a detected issue and requires explicit confirmation', async () => {
        mockFetch((url, init = {}) => {
            if (url === '/api/damage-cases') {
                return jsonResponse({ cases: [reviewCase], needs_review: 1 })
            }
            if (url === '/api/damage-cases/damage-1/confirm' && init.method === 'POST') {
                return jsonResponse({ ...reviewCase, status: 'approved' })
            }
            throw new Error(`Unexpected request: ${init.method || 'GET'} ${url}`)
        })

        renderWithProviders(<DamageView />)

        const tracking = await screen.findByText('A0052HFZF6')
        const row = tracking.closest('tr')
        expect(within(row).getByText('do sprawdzenia')).toBeInTheDocument()
        expect(within(row).getByRole('button', { name: 'Potwierdź uszkodzenie' })).toBeInTheDocument()

        await userEvent.click(within(row).getByRole('button', { name: 'Szczegóły' }))
        expect(screen.getByText('Parcel has been damaged')).toBeInTheDocument()
        expect(screen.getByText('Kod zdarzenia')).toBeInTheDocument()
        expect(screen.queryByText(/"description"/)).not.toBeInTheDocument()

        await userEvent.click(within(row).getByRole('button', { name: 'Potwierdź uszkodzenie' }))

        expect(await screen.findByText('potwierdzone')).toBeInTheDocument()
        expect(screen.getByText('Uszkodzenie potwierdzone.')).toBeInTheDocument()
    })

    it('allows reviewing and editing the email before a separate send action', async () => {
        const readyCase = {
            ...reviewCase,
            status: 'replacement_created',
            replacement_tracking_number: 'A0052NEW123',
            email_draft: {
                from: 'info@wodahumio.pl',
                to: 'jan@example.com',
                subject: 'Wysyłamy ponownie Twoje zamówienie 1648',
                body: 'Pierwotna treść',
            },
        }
        mockFetch((url, init = {}) => {
            if (url === '/api/damage-cases') {
                return jsonResponse({ cases: [readyCase], needs_review: 0 })
            }
            if (url === '/api/damage-cases/damage-1/email-draft' && init.method === 'PATCH') {
                const body = JSON.parse(init.body)
                return jsonResponse({
                    case: { ...readyCase, email_draft: { ...readyCase.email_draft, ...body } },
                    email_draft: body,
                })
            }
            if (url === '/api/damage-cases/damage-1/send-email' && init.method === 'POST') {
                return jsonResponse({
                    case: {
                        ...readyCase,
                        status: 'customer_notified',
                        email_sent_at: '2026-07-16T09:00:00Z',
                    },
                })
            }
            throw new Error(`Unexpected request: ${init.method || 'GET'} ${url}`)
        })

        renderWithProviders(<DamageView />)
        await screen.findByText('A0052HFZF6')
        await userEvent.click(screen.getByRole('button', { name: 'Szczegóły' }))

        const subject = screen.getByLabelText('Temat wiadomości')
        await userEvent.clear(subject)
        await userEvent.type(subject, 'Sprawdzony temat')
        await userEvent.click(screen.getByRole('button', { name: 'Zapisz draft' }))

        expect(await screen.findByText('Zapisano zmiany w wiadomości.')).toBeInTheDocument()
        await userEvent.click(screen.getByRole('button', { name: 'Wyślij e-mail' }))
        expect(await screen.findByText('klient powiadomiony')).toBeInTheDocument()
    })

    it('refreshes provider data only when the operator asks', async () => {
        let refreshed = false
        mockFetch((url, init = {}) => {
            if (url === '/api/damage-cases') {
                return jsonResponse({ cases: refreshed ? [reviewCase] : [], needs_review: refreshed ? 1 : 0 })
            }
            if (url === '/api/damage-cases/refresh' && init.method === 'POST') {
                refreshed = true
                return jsonResponse({ needs_review: 1 })
            }
            throw new Error(`Unexpected request: ${init.method || 'GET'} ${url}`)
        })

        renderWithProviders(<DamageView />)
        expect(await screen.findByText('Brak wykrytych uszkodzeń.')).toBeInTheDocument()
        await userEvent.click(screen.getByRole('button', { name: /Pobierz powiadomienia/ }))
        expect(await screen.findByText('A0052HFZF6')).toBeInTheDocument()
    })
})

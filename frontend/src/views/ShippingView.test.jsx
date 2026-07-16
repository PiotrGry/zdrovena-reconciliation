import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import ShippingView from './ShippingView'
import { deferred, jsonResponse, mockFetch } from '../test/http'
import { renderWithProviders } from '../test/render'

function draft(overrides = {}) {
    return {
        id: 'draft-1',
        source: 'shopify',
        external_order_id: '1001',
        shopify_order_number: '1001',
        customer_name: 'Anna Nowak',
        receiver: {
            first_name: 'Anna',
            last_name: 'Nowak',
            email: 'anna@example.com',
            phone: '+48600111222',
            locker_id: '',
        },
        shipping_address: {
            street: 'Prosta',
            building_number: '1',
            flat_number: '',
            city: 'Warszawa',
            post_code: '00-001',
        },
        order_items: [{ name: 'HUMIO PET', quantity: 1 }],
        packages_count: 1,
        packages_breakdown: [{ type: '1-pak', qty: 1 }],
        courier: 'inpost',
        service: 'inpost_courier_standard',
        status: 'pending',
        pickup_ordered: false,
        created_at: '2026-07-15T10:00:00Z',
        order_date: '2026-07-15T10:00:00Z',
        ...overrides,
    }
}

function installShippingFetch({
    drafts = [],
    afterSyncDrafts,
    syncDeferred,
    errorEnvelope,
    apaczkaServices = [],
} = {}) {
    let draftsCalls = 0
    let confirmCalls = 0
    const updateDraftCalls = []
    const fetchMock = mockFetch((url, init = {}) => {
        if (url === '/api/shipping/apaczka-services') return jsonResponse({ services: apaczkaServices })
        if (url === '/api/shipping/sync') {
            return syncDeferred
                ? syncDeferred.promise.then(() => jsonResponse({
                    allegro: { fetched: 0, created: 0, updated: 1, unchanged: 0, errors: 0 },
                    shopify: { fetched: 0, created: 0, updated: 0, unchanged: 0, errors: 0 },
                }))
                : jsonResponse({ allegro: {}, shopify: {} })
        }
        if (url.includes('/confirm') && init.method === 'POST') {
            confirmCalls += 1
            return jsonResponse({ status: 'created' })
        }
        if (url.startsWith('/api/shipping/drafts/') && init.method === 'PATCH') {
            updateDraftCalls.push(JSON.parse(init.body || '{}'))
            return jsonResponse({ status: 'pending' })
        }
        if (url === '/api/shipping/drafts') {
            draftsCalls += 1
            if (errorEnvelope) return jsonResponse(errorEnvelope, { status: 500 })
            const currentDrafts = afterSyncDrafts && draftsCalls > 1 ? afterSyncDrafts : drafts
            return jsonResponse({ drafts: currentDrafts })
        }
        throw new Error(`Unexpected request: ${init.method || 'GET'} ${url}`)
    })
    return { fetchMock, getConfirmCalls: () => confirmCalls, getUpdateDraftCalls: () => updateDraftCalls }
}

describe('ShippingView', () => {
    it('shows loading and then the empty state', async () => {
        installShippingFetch({ drafts: [] })

        renderWithProviders(<ShippingView />)

        expect(screen.getByText('Ładowanie…')).toBeInTheDocument()
        expect(await screen.findByText('Brak draftów wysyłek.')).toBeInTheDocument()
    })

    it('renders drafts and displays delivery address with missing optional flat number', async () => {
        installShippingFetch({ drafts: [draft()] })

        renderWithProviders(<ShippingView />)
        await screen.findByText('Anna Nowak')
        await userEvent.click(screen.getByRole('button', { name: 'Rozwiń' }))

        const addressLabel = screen.getByText('Adres dostawy')
        expect(addressLabel).toBeInTheDocument()
        expect(addressLabel.nextElementSibling).toHaveTextContent('Prosta 1')
        expect(addressLabel.nextElementSibling).toHaveTextContent('00-001 Warszawa')
    })

    it('shows Apaczka shipping service match status and source', async () => {
        const { getUpdateDraftCalls } = installShippingFetch({
            apaczkaServices: [
                { service_id: '21', label: 'DPD Kurier' },
                { service_id: '53', label: 'ORLEN Paczka' },
            ],
            drafts: [draft({
                courier: 'apaczka',
                service: 'apaczka',
                apaczka_service_id: '21',
                shipping_service_match_status: 'auto_matched',
                shipping_service_match_source: 'Apaczka DPD',
            })],
        })

        renderWithProviders(<ShippingView />)
        await screen.findByText('Anna Nowak')
        await userEvent.click(screen.getByRole('button', { name: 'Rozwiń' }))

        expect(screen.getByText('Dopasowano automatycznie')).toBeInTheDocument()
        expect(screen.getByText('Źródło: Apaczka DPD')).toBeInTheDocument()
        expect(screen.getAllByText(/DPD Kurier/).length).toBeGreaterThan(0)

        const select = screen.getByDisplayValue('DPD Kurier')
        expect(select).toHaveValue('21')
        const save = screen.getByRole('button', { name: 'Zapisz' })
        expect(save).toBeDisabled()

        await userEvent.selectOptions(select, '53')
        expect(save).toBeEnabled()
        await userEvent.click(save)

        expect(getUpdateDraftCalls()).toEqual([
            { apaczka_service_id: '53', reviewed: true },
        ])
    })

    it('distinguishes pickup point delivery from a street address', async () => {
        installShippingFetch({
            drafts: [draft({
                service: 'inpost_locker_standard',
                receiver: {
                    first_name: 'Jan',
                    last_name: 'Kowalski',
                    email: 'jan@example.com',
                    phone: '+48600111222',
                    locker_id: 'WAW123A',
                },
                shipping_address: {
                    street: 'Skrytka',
                    building_number: '9',
                    flat_number: 'WAW123A',
                    city: 'Warszawa',
                    post_code: '00-001',
                },
            })],
        })

        renderWithProviders(<ShippingView />)
        await screen.findByText('Anna Nowak')
        await userEvent.click(screen.getByRole('button', { name: 'Rozwiń' }))

        expect(screen.getByText('Paczkomat')).toBeInTheDocument()
        expect(screen.getByText('WAW123A')).toBeInTheDocument()
        expect(screen.queryByText('Skrytka 9 WAW123A')).not.toBeInTheDocument()
    })

    it('sorts visible drafts by package count', async () => {
        installShippingFetch({
            drafts: [
                draft({ id: 'three', shopify_order_number: '1003', customer_name: 'Trzy Paczki', packages_count: 3, packages_breakdown: [{ type: '1-pak', qty: 3 }] }),
                draft({ id: 'one', shopify_order_number: '1001', customer_name: 'Jedna Paczka', packages_count: 1, packages_breakdown: [{ type: '1-pak', qty: 1 }] }),
            ],
        })

        renderWithProviders(<ShippingView />)
        await screen.findByText('Trzy Paczki')
        await userEvent.click(screen.getByRole('button', { name: /^Paczki$/ }))

        const orderNumbers = screen.getAllByText(/^#100[13]$/).map(node => node.textContent)
        expect(orderNumbers).toEqual(['#1001', '#1003'])
    })

    it('disables sync while pending and updates visible state after success', async () => {
        const syncRequest = deferred()
        installShippingFetch({
            drafts: [draft({ status: 'pending' })],
            afterSyncDrafts: [draft({ status: 'created' })],
            syncDeferred: syncRequest,
        })

        renderWithProviders(<ShippingView />)
        await screen.findByText('oczekujące')

        const syncButton = screen.getByRole('button', { name: /Synchronizuj/ })
        await userEvent.click(syncButton)

        expect(syncButton).toBeDisabled()
        expect(screen.getByRole('button', { name: /Synchronizowanie/ })).toBeDisabled()

        await act(async () => {
            syncRequest.resolve()
            await syncRequest.promise
        })

        await waitFor(() => {
            expect(screen.getAllByText('nadane').some(node => node.closest('.pill'))).toBe(true)
        })
        expect(screen.getByText(/Synchronizacja zakończona/)).toBeInTheDocument()
    })

    it('keeps previous drafts visible and shows a safe Polish error with correlation id', async () => {
        installShippingFetch({
            drafts: [draft()],
            errorEnvelope: {
                message_pl: 'Nie udało się wczytać wysyłek.',
                correlation_id: 'corr-shipping-1',
                details: 'Traceback SECRET_TOKEN=hidden',
            },
        })

        renderWithProviders(<ShippingView />)

        expect(await screen.findByText(/Nie udało się wczytać wysyłek/)).toBeInTheDocument()
        expect(screen.getByText(/corr-shipping-1/)).toBeInTheDocument()
        expect(screen.queryByText(/SECRET_TOKEN|Traceback/)).not.toBeInTheDocument()
    })

    it('polls pending Allegro confirmation and refreshes after it reaches a terminal state', async () => {
        const pending = draft({ id: 'pending-1', status: 'pending_confirmation' })
        const created = draft({ id: 'pending-1', status: 'created' })
        const { getConfirmCalls } = installShippingFetch({
            drafts: [pending],
            afterSyncDrafts: [created],
        })

        renderWithProviders(<ShippingView />)
        await screen.findByText('czeka na Allegro')

        await act(async () => {
            await new Promise(resolve => setTimeout(resolve, 5100))
        })

        await waitFor(() => {
            expect(screen.getAllByText('nadane').some(node => node.closest('.pill'))).toBe(true)
        })
        expect(getConfirmCalls()).toBe(1)
    }, 7000)
})

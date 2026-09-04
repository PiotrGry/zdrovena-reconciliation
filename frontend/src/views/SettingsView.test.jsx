import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import SettingsView from './SettingsView'
import { jsonResponse, mockFetch } from '../test/http'
import { renderWithProviders } from '../test/render'

describe('SettingsView', () => {
    it('offers the recipient phone change the draft row no longer allows', async () => {
        mockFetch(url => {
            if (url.startsWith('/api/integrations/health')) return jsonResponse({ integrations: [] })
            return jsonResponse({})
        })

        renderWithProviders(<SettingsView />)

        expect(await screen.findByText('Telefon odbiorcy w zamówieniu')).toBeInTheDocument()
        expect(screen.getByLabelText('Numer zamówienia')).toBeInTheDocument()
    })
})

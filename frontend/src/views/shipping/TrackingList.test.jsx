import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TrackingList } from './TrackingList'

describe('TrackingList', () => {
    it('renders one number per parcel with its position', () => {
        render(<TrackingList draft={{
            tracking_number: '620A',
            courier_shipments: [
                { id: 's1', tracking_number: '620A', package_type: '2-pak', package_number: '1' },
                { id: 's2', tracking_number: '620B', package_type: '2-pak', package_number: '2' },
                { id: 's3', tracking_number: '620C', package_type: 'szkło', package_number: '1' },
            ],
        }} />)

        expect(screen.getByText('620A')).toBeInTheDocument()
        expect(screen.getByText('620B')).toBeInTheDocument()
        expect(screen.getByText('620C')).toBeInTheDocument()
        expect(screen.getByText('Numery śledzenia (3)')).toBeInTheDocument()
        expect(screen.getByText('2-pak 1/2')).toBeInTheDocument()
        expect(screen.getByText('szkło 1/1')).toBeInTheDocument()
    })

    it('falls back to the single number on drafts that predate courier_shipments', () => {
        render(<TrackingList draft={{ tracking_number: '620LEGACY', courier_shipments: [] }} />)

        expect(screen.getByText('620LEGACY')).toBeInTheDocument()
        expect(screen.getByText('Numer śledzenia')).toBeInTheDocument()
    })

    it('renders a dash when nothing has a number yet', () => {
        render(<TrackingList draft={{ tracking_number: null, courier_shipments: [] }} />)

        expect(screen.getByText('—')).toBeInTheDocument()
    })

    it('skips parcels the carrier has not numbered yet', () => {
        render(<TrackingList draft={{
            tracking_number: '620A',
            courier_shipments: [
                { id: 's1', tracking_number: '620A', package_type: '1-pak', package_number: '1' },
                { id: 's2', tracking_number: '', package_type: '1-pak', package_number: '2' },
            ],
        }} />)

        expect(screen.getByText('Numery śledzenia (1)')).toBeInTheDocument()
        expect(screen.getByText('1 z 2 paczek czeka na numer')).toBeInTheDocument()
    })
})

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { CodSplit } from './CodSplit'

describe('CodSplit', () => {
    it('shows what each parcel collects', () => {
        render(
            <CodSplit
                draft={{
                    cod: { amount: '700.00', currency: 'PLN' },
                    cod_split: ['600.00', '100.00'],
                    cod_split_basis: 'value',
                }} />,
        )

        expect(screen.getByText('Paczka 1')).toBeInTheDocument()
        expect(screen.getByText('600.00 PLN')).toBeInTheDocument()
        expect(screen.getByText('Paczka 2')).toBeInTheDocument()
        expect(screen.getByText('100.00 PLN')).toBeInTheDocument()
    })

    it('says so when the split is even rather than by value', () => {
        // A draft created before line values were stored. The total is still
        // right, but the operator should know the division is a guess.
        render(
            <CodSplit
                draft={{
                    cod: { amount: '300.00', currency: 'PLN' },
                    cod_split: ['150.00', '150.00'],
                    cod_split_basis: 'equal',
                }} />,
        )

        expect(screen.getByText('podział równy — brak cen pozycji')).toBeInTheDocument()
    })

    it('does not call an even split a value split', () => {
        render(
            <CodSplit
                draft={{
                    cod: { amount: '700.00', currency: 'PLN' },
                    cod_split: ['600.00', '100.00'],
                    cod_split_basis: 'value',
                }} />,
        )

        expect(screen.queryByText('podział równy — brak cen pozycji')).not.toBeInTheDocument()
    })

    it('shows why a draft cannot be split instead of staying silent', () => {
        render(
            <CodSplit
                draft={{
                    cod: { amount: '300.00', currency: 'PLN' },
                    cod_split_error: 'Parcel 2 of 2 would collect 0.00',
                }} />,
        )

        expect(screen.getByText(/would collect 0.00/)).toBeInTheDocument()
    })

    it('renders nothing for a single-parcel order', () => {
        const { container } = render(
            <CodSplit draft={{ cod: { amount: '300.00', currency: 'PLN' } }} />,
        )

        expect(container).toBeEmptyDOMElement()
    })

    it('renders nothing for an order that is not collect-on-delivery', () => {
        const { container } = render(<CodSplit draft={{}} />)

        expect(container).toBeEmptyDOMElement()
    })
})

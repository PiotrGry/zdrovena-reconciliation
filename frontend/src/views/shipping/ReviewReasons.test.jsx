import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ReviewReasonChips, reviewReasonLabel } from './ReviewReasons'

const T = {
    sh_review_missing_phone: 'brak poprawnego telefonu',
    sh_review_cod_error: 'nieczytelna kwota pobrania',
}

describe('reviewReasonLabel', () => {
    it('translates a known code', () => {
        expect(reviewReasonLabel('missing_phone', T)).toBe('brak poprawnego telefonu')
    })

    it('falls back to the raw code when nobody has named it yet', () => {
        // A reason added on the server must stay visible here. A blank chip
        // would hide the very thing the operator is being asked to look at.
        expect(reviewReasonLabel('some_new_reason', T)).toBe('some_new_reason')
    })

    it('survives a missing translation table', () => {
        expect(reviewReasonLabel('missing_phone')).toBe('missing_phone')
    })
})

describe('ReviewReasonChips', () => {
    it('renders one chip per reason', () => {
        render(<ReviewReasonChips reasons={['missing_phone', 'cod_error']} T={T} />)

        expect(screen.getByText('brak poprawnego telefonu')).toBeInTheDocument()
        expect(screen.getByText('nieczytelna kwota pobrania')).toBeInTheDocument()
    })

    it('renders nothing for a draft stored before the field existed', () => {
        const { container } = render(<ReviewReasonChips reasons={undefined} T={T} />)
        expect(container).toBeEmptyDOMElement()
    })

    it('renders nothing for an empty list', () => {
        const { container } = render(<ReviewReasonChips reasons={[]} T={T} />)
        expect(container).toBeEmptyDOMElement()
    })
})

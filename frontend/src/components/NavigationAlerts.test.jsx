import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Header } from './Header'
import Sidebar from './Sidebar'
import { renderWithProviders } from '../test/render'

describe('damage navigation indicators', () => {
    it('marks the damage bell as active and still handles clicks', async () => {
        const onDamageClick = vi.fn()
        renderWithProviders(
            <Header damageCount={3} damageActive onDamageClick={onDamageClick} />
        )

        const bell = screen.getByRole('button', { name: 'Uszkodzone przesyłki: 3' })
        expect(bell).toHaveAttribute('aria-current', 'page')
        await userEvent.click(bell)
        expect(onDamageClick).toHaveBeenCalledOnce()
    })

    it('uses distinct icons for damages and the error queue', () => {
        renderWithProviders(<Sidebar page="files" onNavigate={() => {}} damageCount={3} />)

        const damagePath = screen.getByRole('button', { name: /Uszkodzenia/ })
            .querySelector('svg path').getAttribute('d')
        const dlqPath = screen.getByRole('button', { name: /Kolejka błędów/ })
            .querySelector('svg path').getAttribute('d')
        expect(damagePath).not.toBe(dlqPath)
    })
})

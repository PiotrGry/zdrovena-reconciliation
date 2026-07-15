import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { usePolling } from './usePolling'

function PollingProbe({ onTick, enabled = true }) {
    usePolling(onTick, 1000, { enabled })
    return <div>polling</div>
}

describe('usePolling', () => {
    it('polls while enabled and stops after unmount', () => {
        vi.useFakeTimers()
        const onTick = vi.fn()

        const { unmount } = render(<PollingProbe onTick={onTick} />)

        vi.advanceTimersByTime(1000)
        expect(onTick).toHaveBeenCalledTimes(1)

        unmount()
        vi.advanceTimersByTime(3000)
        expect(onTick).toHaveBeenCalledTimes(1)
    })

    it('does not poll when disabled', () => {
        vi.useFakeTimers()
        const onTick = vi.fn()

        render(<PollingProbe onTick={onTick} enabled={false} />)

        vi.advanceTimersByTime(3000)
        expect(onTick).not.toHaveBeenCalled()
    })
})

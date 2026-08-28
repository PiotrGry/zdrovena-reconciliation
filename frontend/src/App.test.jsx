import { describe, expect, it } from 'vitest'

import { VIEWS } from './App'
import { FEATURES } from './features'

describe('view routing', () => {
    it('keeps the unfinished cost module out of the router', () => {
        // CostView is a placeholder ("Moduł w przygotowaniu"). It had no sidebar
        // entry, but it stayed in VIEWS unguarded, so a stale zdrovena_page in
        // localStorage still landed the operator on a dead screen (#314).
        expect(FEATURES.costs).toBe(false)
        expect(VIEWS).not.toHaveProperty('costs')
    })

    it('routes to no view whose feature flag is off', () => {
        // The invariant, so the next placeholder cannot slip in the same way.
        for (const [name, enabled] of Object.entries(FEATURES)) {
            if (name.startsWith('kpi_')) continue
            if (!enabled) expect(VIEWS).not.toHaveProperty(name)
        }
    })

    it('still routes to the views that are switched on', () => {
        // Guards against "fix" by deleting everything.
        for (const [name, enabled] of Object.entries(FEATURES)) {
            if (name.startsWith('kpi_')) continue
            if (enabled) expect(VIEWS).toHaveProperty(name)
        }
        expect(VIEWS).toHaveProperty('files')
        expect(VIEWS).toHaveProperty('settings')
    })
})

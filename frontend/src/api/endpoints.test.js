// The typed endpoint layer is only worth having if views actually go through it
// (issue #318). Nothing fails when a view reaches for `fetchJson` directly — the
// request still works — so only a rule notices the drift.

import { describe, it, expect } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = path.dirname(path.dirname(fileURLToPath(import.meta.url)))

function walk(dir) {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
        const full = path.join(dir, entry.name)
        if (entry.isDirectory()) return walk(full)
        return /\.(js|jsx|ts)$/.test(entry.name) ? [full] : []
    })
}

// The areas #318 covers. FilesView is a generic blob browser over arbitrary
// keys and is deliberately not in scope — widening this rule to it would be
// scope the issue did not ask for.
const IN_SCOPE = /views\/(ShippingView|DamageView|CloseView|SettingsView)|views\/(shipping|close)\//

const VIEWS = walk(path.join(SRC, 'views')).filter(
    f => !/\.test\./.test(f) && IN_SCOPE.test(f.replace(/\\/g, '/')),
)

describe('views go through the typed endpoint layer', () => {
    it('no view calls fetchJson directly for Shipping, Damage, Close or Settings', () => {
        const offenders = VIEWS.filter(file => {
            const source = fs.readFileSync(file, 'utf8')
            return /\bfetchJson\s*\(/.test(source)
        }).map(f => path.relative(SRC, f))

        expect(offenders).toEqual([])
    })
})

describe('endpoint types come from the generated schema', () => {
    const endpoints = fs.readFileSync(path.join(SRC, 'api', 'endpoints.ts'), 'utf8')

    it('derives response types from the OpenAPI paths type', () => {
        expect(endpoints).toContain("import type { paths } from './generated/schema'")
    })

    it('does not hand-write a response shape for a contracted endpoint', () => {
        // Every exported response type must be projected out of `paths`.
        // A literal object type here would be a second source of truth that
        // drifts silently from the backend.
        const declared = [...endpoints.matchAll(/export type (\w+Response)\s*=\s*([^\n]*)/g)]

        expect(declared.length).toBeGreaterThan(0)
        const handWritten = declared
            .filter(([, , rhs]) => !rhs.trim().startsWith('paths[') && !rhs.trim().startsWith('{'))
            .map(([, name]) => name)

        expect(handWritten).toEqual([])
    })
})

import { vi } from 'vitest'

export function jsonResponse(body, { status = 200 } = {}) {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    })
}

export function emptyResponse({ status = 204 } = {}) {
    return new Response(null, { status })
}

export function deferred() {
    let resolve
    let reject
    const promise = new Promise((res, rej) => {
        resolve = res
        reject = rej
    })
    return { promise, resolve, reject }
}

export function mockFetch(handler) {
    return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init = {}) => {
        const url = typeof input === 'string' ? input : input.url
        return Promise.resolve(handler(url, init))
    })
}

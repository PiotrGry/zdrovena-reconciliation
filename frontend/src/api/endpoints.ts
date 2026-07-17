import { fetchJson } from '../api.js'
import type { paths } from './generated/schema'

type FetchOptions = RequestInit & { token?: string }
type JsonFetch = <T>(url: string, options?: FetchOptions) => Promise<T>
const request = fetchJson as JsonFetch

export type HealthResponse =
    paths['/health']['get']['responses'][200]['content']['application/json']
export type SalesInvoicesResponse =
    paths['/api/invoices/sales']['get']['responses'][200]['content']['application/json']
export type ProductsResponse =
    paths['/api/invoices/products']['get']['responses'][200]['content']['application/json']
export type ShippingDraftsResponse =
    paths['/api/shipping/drafts']['get']['responses'][200]['content']['application/json']
export type DlqEntriesResponse =
    paths['/api/shipping/drafts/dlq']['get']['responses'][200]['content']['application/json']
export type ShippingSyncResponse =
    paths['/api/shipping/sync']['post']['responses'][200]['content']['application/json']
export type DlqRetryResponse =
    paths['/api/shipping/drafts/dlq/{entry_id}/retry']['post']['responses'][200]['content']['application/json']

export type DamageCase = Record<string, unknown> & {
    id: string
    status: string
    tracking_number?: string | null
    order_number?: string | null
    customer_name?: string | null
    sources?: string[]
    evidence?: Array<Record<string, unknown>>
    email_draft?: Record<string, unknown> | null
}

export type DamageCasesResponse = { cases: DamageCase[]; needs_review: number }
export type DamageSummaryResponse = { needs_review: number }
export type DamageActionResponse =
    | DamageCase
    | { case: DamageCase; draft?: Record<string, unknown>; email_draft?: Record<string, unknown> }

export function getHealth(): Promise<HealthResponse> {
    return request<HealthResponse>('/health')
}

export function getSalesInvoices({
    year,
    month,
    token,
}: {
    year: number
    month: number
    token: string
}): Promise<SalesInvoicesResponse> {
    return request<SalesInvoicesResponse>(`/api/invoices/sales?year=${year}&month=${month}`, { token })
}

export function getProducts({
    activeOnly,
    token,
}: {
    activeOnly: boolean
    token: string
}): Promise<ProductsResponse> {
    const query = activeOnly ? '?active_only=true' : ''
    return request<ProductsResponse>(`/api/invoices/products${query}`, { token })
}

export function getShippingDrafts({ token }: { token: string }): Promise<ShippingDraftsResponse> {
    return request<ShippingDraftsResponse>('/api/shipping/drafts', { token })
}

export function syncShipping({ token }: { token: string }): Promise<ShippingSyncResponse> {
    return request<ShippingSyncResponse>('/api/shipping/sync', { method: 'POST', token })
}

export function getDlqEntries({ token }: { token: string }): Promise<DlqEntriesResponse> {
    return request<DlqEntriesResponse>('/api/shipping/drafts/dlq', { token })
}

export function retryDlqEntry({
    id,
    token,
}: {
    id: string
    token: string
}): Promise<DlqRetryResponse> {
    return request<DlqRetryResponse>(`/api/shipping/drafts/dlq/${id}/retry`, {
        method: 'POST',
        token,
    })
}

export function discardDlqEntry({ id, token }: { id: string; token: string }): Promise<void> {
    return request<void>(`/api/shipping/drafts/dlq/${id}`, { method: 'DELETE', token })
}

export function getDamageCases({ token }: { token: string }): Promise<DamageCasesResponse> {
    return request<DamageCasesResponse>('/api/damage-cases', { token })
}

export function getDamageSummary({ token }: { token: string }): Promise<DamageSummaryResponse> {
    return request<DamageSummaryResponse>('/api/damage-cases/summary', { token })
}

export function refreshDamageCases({ token }: { token: string }): Promise<Record<string, unknown>> {
    return request<Record<string, unknown>>('/api/damage-cases/refresh', { method: 'POST', token })
}

export function damageAction({
    id,
    action,
    token,
    body,
    method = 'POST',
}: {
    id: string
    action: string
    token: string
    body?: Record<string, unknown>
    method?: 'POST' | 'PATCH'
}): Promise<DamageActionResponse> {
    return request<DamageActionResponse>(`/api/damage-cases/${id}/${action}`, {
        method,
        token,
        ...(body
            ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
            : {}),
    })
}

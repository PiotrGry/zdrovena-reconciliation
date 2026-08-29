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

// The damage endpoints return `dict[str, Any]` on the backend, so the generated
// schema gives `{ [key: string]: unknown }` and there is no shape to project.
// `DamageCase` narrows that for the fields the UI reads; it is a local
// assumption, not a contract, and it is annotated as such on purpose. Removing
// it would lose editor help without gaining safety, and inventing a fuller type
// would be a hand-duplicated contract that drifts silently.
//
// The real fix is response models on the backend — see the #318 PR note.
export type DamageActionResponse =
    paths['/api/damage-cases/{case_id}/confirm']['post']['responses'][200]['content']['application/json']

export type DamageCase = DamageActionResponse & {
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

// ── Close workflow ────────────────────────────────────────────────────────────
//
// These endpoints publish real response models, so the type comes from the
// generated schema rather than being written out again here (issue #318).

export type CloseWorkflowRun =
    paths['/api/close/workflow']['get']['responses'][200]['content']['application/json']
export type CloseHistoryResponse =
    paths['/api/close/history']['get']['responses'][200]['content']['application/json']
export type IntegrationsHealthResponse =
    paths['/api/integrations/health']['get']['responses'][200]['content']['application/json']

export function getCloseWorkflow({
    year,
    month,
    token,
}: {
    year: number
    month: number
    token: string
}): Promise<CloseWorkflowRun> {
    return request<CloseWorkflowRun>(`/api/close/workflow?year=${year}&month=${month}`, { token })
}

export function runCloseAction({
    action,
    body,
    token,
}: {
    action: string
    body: unknown
    token: string
}): Promise<CloseWorkflowRun> {
    return request<CloseWorkflowRun>(`/api/close/workflow/actions/${action}`, {
        method: 'POST',
        token,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
}

export function addCloseWaiver({
    body,
    token,
}: {
    body: unknown
    token: string
}): Promise<CloseWorkflowRun> {
    return request<CloseWorkflowRun>('/api/close/workflow/waivers', {
        method: 'POST',
        token,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
}

export function removeCloseWaiver({
    query,
    token,
}: {
    query: string
    token: string
}): Promise<CloseWorkflowRun> {
    return request<CloseWorkflowRun>(`/api/close/workflow/waivers?${query}`, {
        method: 'DELETE',
        token,
    })
}

export function resetCloseWorkflow({
    body,
    token,
}: {
    body: unknown
    token: string
}): Promise<CloseWorkflowRun> {
    return request<CloseWorkflowRun>('/api/close/workflow/reset', {
        method: 'POST',
        token,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
}

export function getCloseHistory({
    limit,
    token,
}: {
    limit: number
    token: string
}): Promise<CloseHistoryResponse> {
    return request<CloseHistoryResponse>(`/api/close/history?limit=${limit}`, { token })
}

export function deleteCloseHistoryEntry({
    timestamp,
    token,
}: {
    timestamp: string
    token: string
}): Promise<void> {
    return request<void>(`/api/close/history/${encodeURIComponent(timestamp)}`, {
        method: 'DELETE',
        token,
    })
}

export function getIntegrationsHealth({
    runChecks,
    token,
}: {
    runChecks?: boolean
    token: string
}): Promise<IntegrationsHealthResponse> {
    const suffix = runChecks ? '?run_checks=true' : ''
    return request<IntegrationsHealthResponse>(`/api/integrations/health${suffix}`, { token })
}

// ── Files ─────────────────────────────────────────────────────────────────────
//
// No response body by contract, so the return type is void rather than an
// invented shape.

export function uploadFile({
    key,
    body,
    contentType,
    token,
}: {
    key: string
    body: BodyInit
    contentType: string
    token: string
}): Promise<void> {
    return request<void>(`/api/files/${encodeURIComponent(key)}`, {
        method: 'PUT',
        token,
        headers: { 'Content-Type': contentType },
        body,
    })
}

export function deleteFile({ key, token }: { key: string; token: string }): Promise<void> {
    return request<void>(`/api/files/${encodeURIComponent(key)}`, { method: 'DELETE', token })
}

export function listFiles({ prefix, token }: { prefix: string; token: string }): Promise<unknown> {
    return request<unknown>(`/api/files?prefix=${encodeURIComponent(prefix)}`, { token })
}

// ── Shipping invoices ─────────────────────────────────────────────────────────
//
// These two endpoints return `dict[str, Any]` on the backend, so the generated
// schema has no shape to project. The type below is what the contract actually
// promises today — see the note in the #318 PR about the backend response models
// that would let this be narrowed honestly.

export type InvoicePreviewResponse =
    paths['/api/shipping/drafts/{draft_id}/invoice-preview']['get']['responses'][200]['content']['application/json']
export type CreateInvoiceResponse =
    paths['/api/shipping/drafts/{draft_id}/create-invoice']['post']['responses'][200]['content']['application/json']

export function getInvoicePreview({
    draftId,
    token,
    signal,
}: {
    draftId: string
    token: string
    // The panel aborts an in-flight preview when the operator closes it.
    signal?: AbortSignal
}): Promise<InvoicePreviewResponse> {
    return request<InvoicePreviewResponse>(`/api/shipping/drafts/${draftId}/invoice-preview`, {
        token,
        signal,
    })
}

export function createDraftInvoice({
    draftId,
    token,
}: {
    draftId: string
    token: string
}): Promise<CreateInvoiceResponse> {
    // No request body by contract; sending one would change what the backend sees.
    return request<CreateInvoiceResponse>(`/api/shipping/drafts/${draftId}/create-invoice`, {
        method: 'POST',
        token,
    })
}

export const SHIPPING_TABLE_WIDTHS_KEY = 'shipping.table.columnWidths.v1'

export const SHIPPING_COLUMNS = [
    { id: 'order', label: 'Nr', width: 190, minWidth: 150, sortable: true },
    { id: 'source', label: 'Źródło', width: 96, minWidth: 84, sortable: true },
    { id: 'customer', label: 'Klient', width: 210, minWidth: 150, sortable: true },
    { id: 'email', label: 'Email', width: 180, minWidth: 140 },
    { id: 'phone', label: 'Telefon', width: 128, minWidth: 112 },
    { id: 'packages', label: 'Paczki', width: 112, minWidth: 92, sortable: true },
    { id: 'courier', label: 'Kurier', width: 150, minWidth: 124, sortable: true },
    { id: 'date', label: 'Data', width: 142, minWidth: 124, sortable: true },
    { id: 'status', label: 'Status', width: 104, minWidth: 92, sortable: true },
    { id: 'pickup', label: 'Podjazd', width: 82, minWidth: 74 },
]

export function defaultColumnWidths() {
    return Object.fromEntries(SHIPPING_COLUMNS.map(column => [column.id, column.width]))
}

export function mergeColumnWidths(saved = {}) {
    const widths = defaultColumnWidths()
    for (const column of SHIPPING_COLUMNS) {
        const value = Number(saved[column.id])
        if (Number.isFinite(value)) {
            widths[column.id] = Math.max(column.minWidth, value)
        }
    }
    return widths
}

export function loadColumnWidths(storage = window.localStorage) {
    try {
        return mergeColumnWidths(JSON.parse(storage.getItem(SHIPPING_TABLE_WIDTHS_KEY) || '{}'))
    } catch {
        return defaultColumnWidths()
    }
}

export function shippingGridTemplate(columnWidths) {
    return SHIPPING_COLUMNS
        .map(column => `minmax(${column.minWidth}px, ${columnWidths[column.id] || column.width}px)`)
        .join(' ')
}

export function packagesSortValue(draft) {
    if (Number.isFinite(draft.packages_count)) return draft.packages_count
    if (Array.isArray(draft.packages_breakdown)) {
        return draft.packages_breakdown.reduce((sum, item) => sum + (Number(item.qty) || 0), 0)
    }
    return null
}

export function compareValues(left, right) {
    const leftEmpty = left == null || left === ''
    const rightEmpty = right == null || right === ''
    if (leftEmpty && rightEmpty) return 0
    if (leftEmpty) return 1
    if (rightEmpty) return -1

    const leftNumber = typeof left === 'number' ? left : Number(left)
    const rightNumber = typeof right === 'number' ? right : Number(right)
    if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
        return leftNumber - rightNumber
    }

    return String(left).localeCompare(String(right), 'pl', { numeric: true, sensitivity: 'base' })
}

export function nextSortState(current, columnId) {
    if (current.key !== columnId) return { key: columnId, direction: 'asc' }
    if (current.direction === 'asc') return { key: columnId, direction: 'desc' }
    return { key: null, direction: null }
}

export function sortDrafts(drafts, sortState, getValue) {
    if (!sortState.key || !sortState.direction) return drafts
    const direction = sortState.direction === 'desc' ? -1 : 1
    return drafts
        .map((draft, index) => ({ draft, index }))
        .sort((left, right) => {
            const leftValue = getValue(left.draft, sortState.key)
            const rightValue = getValue(right.draft, sortState.key)
            const leftEmpty = leftValue == null || leftValue === ''
            const rightEmpty = rightValue == null || rightValue === ''
            if (leftEmpty && rightEmpty) return left.index - right.index
            if (leftEmpty) return 1
            if (rightEmpty) return -1
            const result = compareValues(leftValue, rightValue)
            return result === 0 ? left.index - right.index : result * direction
        })
        .map(item => item.draft)
}

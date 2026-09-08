// Search over every attribute a draft carries, not just the two the header
// mentions. The number an operator has in hand is usually the one printed on
// the label — a tracking number, the courier's shipment id, a pickup order id —
// and those live several levels down in the record (courier_shipments[]), where
// the old `order_number || customer_name` filter could never reach them.
//
// Matching is deliberately tiered rather than "fuzzy everywhere": a 24-digit
// tracking number and a customer surname want different tolerance. Whole-field
// and substring hits always outrank a typo hit, and a typo hit has to stay
// compact, so "nowak" never drags in "Natalia Ossowska Walkiewicz".

const SCORE_EQUAL = 100
const SCORE_PREFIX = 80
const SCORE_SUBSTRING = 60
const SCORE_DIGITS = 55
const SCORE_FUZZY_MAX = 40

// Below this length a typo match is noise: two or three letters are a
// subsequence of almost any field, so short queries stay literal.
const MIN_FUZZY_QUERY = 4
// A typo hit may not sprawl: the matched span stays within this multiple of the
// query length. "warszwa" inside "Warszawa" passes; letters picked out of three
// unrelated words do not.
const MAX_FUZZY_SPAN = 1.6
const MIN_DIGITS_QUERY = 3
const NUMERIC_TOKEN = /^[\d+./-]+$/
const MAX_DEPTH = 6

const DIACRITICS = /[̀-ͯ]/g
const STROKED = { ł: 'l', Ł: 'l' }

// Keyed by the draft object: the list is replaced wholesale on every poll, so
// stale entries fall out of the map with the records they described.
const indexCache = new WeakMap()

export function normalize(value) {
    return String(value)
        .replace(/[łŁ]/g, character => STROKED[character])
        .normalize('NFD')
        .replace(DIACRITICS, '')
        .toLowerCase()
        .trim()
}

function digitsOf(value) {
    return value.replace(/\D+/g, '')
}

/** Every primitive a record holds, normalized, deduplicated. */
export function searchableValues(record, depth = 0, collected = new Set()) {
    if (record == null || depth > MAX_DEPTH) return collected
    if (Array.isArray(record)) {
        for (const item of record) searchableValues(item, depth + 1, collected)
        return collected
    }
    if (typeof record === 'object') {
        for (const value of Object.values(record)) searchableValues(value, depth + 1, collected)
        return collected
    }
    // Booleans carry no searchable meaning — "true" would match every draft
    // that has any flag set, which is worse than not matching at all.
    if (typeof record === 'boolean') return collected
    const text = normalize(record)
    if (text) collected.add(text)
    return collected
}

function indexOf(record) {
    const cached = indexCache.get(record)
    if (cached) return cached
    const values = [...searchableValues(record)]
    const index = values.map(text => ({ text, digits: digitsOf(text) }))
    indexCache.set(record, index)
    return index
}

/** Are the query's characters in order and close together inside `text`? */
function fuzzyScore(text, query) {
    if (query.length < MIN_FUZZY_QUERY) return 0
    let start = -1
    let cursor = 0
    for (let position = 0; position < text.length && cursor < query.length; position += 1) {
        if (text[position] !== query[cursor]) continue
        if (cursor === 0) start = position
        cursor += 1
        if (cursor === query.length) {
            const span = position - start + 1
            if (span > query.length * MAX_FUZZY_SPAN) return 0
            // A tighter span is a better match: no gaps at all scores the top.
            const gaps = span - query.length
            return Math.max(1, SCORE_FUZZY_MAX - gaps * 8)
        }
    }
    return 0
}

function scoreToken(index, token) {
    const tokenDigits = digitsOf(token)
    let best = 0
    for (const field of index) {
        let score = 0
        if (field.text === token) score = SCORE_EQUAL
        else if (field.text.startsWith(token)) score = SCORE_PREFIX
        else if (field.text.includes(token)) score = SCORE_SUBSTRING
        else if (
            // A number the operator types rarely carries the separators the
            // record stores ("600111222" vs "+48 600 111 222"), so numeric
            // tokens are also compared digits-to-digits.
            NUMERIC_TOKEN.test(token) &&
            tokenDigits.length >= MIN_DIGITS_QUERY &&
            field.digits.includes(tokenDigits)
        ) {
            score = SCORE_DIGITS
        } else score = fuzzyScore(field.text, token)
        if (score > best) best = score
        if (best === SCORE_EQUAL) break
    }
    return best
}

/** Score one record: 0 when any query token finds nothing. */
export function scoreRecord(record, tokens) {
    let total = 0
    for (const token of tokens) {
        const score = scoreToken(indexOf(record), token)
        if (!score) return 0
        total += score
    }
    return total
}

export function queryTokens(query) {
    return normalize(query ?? '')
        .split(/\s+/)
        .filter(Boolean)
}

/**
 * Filter to the records that match every token, best match first. An empty
 * query is not a search: the caller's own ordering is handed straight back.
 */
export function rankByQuery(records, query) {
    const tokens = queryTokens(query)
    if (!tokens.length) return records
    return records
        .map((record, position) => ({ record, position, score: scoreRecord(record, tokens) }))
        .filter(entry => entry.score > 0)
        .sort((left, right) =>
            right.score - left.score || left.position - right.position)
        .map(entry => entry.record)
}

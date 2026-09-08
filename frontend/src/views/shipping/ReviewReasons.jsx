// Why a draft waits for review, shown next to the status it explains.

/**
 * Turn one reason code into the operator's words.
 *
 * An unknown code renders as itself rather than as nothing: a reason added on
 * the server before anyone names it here is still visible, and a blank chip
 * would be worse than a rough one.
 */
export function reviewReasonLabel(code, T = {}) {
    return T[`sh_review_${code}`] ?? code
}

export function ReviewReasonChips({ reasons, T = {} }) {
    if (!reasons?.length) return null
    return (
        <>
            {reasons.map(code => (
                <span
                    key={code}
                    style={{
                        fontSize: '0.72em', padding: '2px 7px', borderRadius: 4,
                        fontWeight: 500, whiteSpace: 'nowrap',
                        background: 'var(--warn-subtle, #fffbeb)',
                        color: 'var(--warn, #b45309)',
                        border: '1px solid var(--warn-border, #fcd34d)',
                    }}>
                    {reviewReasonLabel(code, T)}
                </span>
            ))}
        </>
    )
}

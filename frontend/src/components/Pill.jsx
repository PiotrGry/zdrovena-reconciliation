export function Pill({ kind = 'default', children }) {
    return (
        <span className={`pill ${kind}`}>
            <span className="dot" />
            {children}
        </span>
    )
}

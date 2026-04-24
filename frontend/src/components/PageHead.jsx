export function PageHead({ title, sub, actions }) {
    return (
        <div className="page-head">
            <div>
                <h1 className="page-title">{title}</h1>
                {sub && <p className="page-sub">{sub}</p>}
            </div>
            {actions && <div className="page-actions">{actions}</div>}
        </div>
    )
}

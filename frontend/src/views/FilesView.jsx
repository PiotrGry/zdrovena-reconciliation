import { useState, useCallback, useRef, useEffect } from "react";
import { useAuth } from "../auth";
import { useT } from "../lang";
import { FEATURES } from "../features";
import { PageHead } from "../components/PageHead";
import { Icon } from "../components/Icon";
import { fmtBytes, fmtDate, MONTHS_PL, PIPELINE_STEPS } from "../data";

function extOf(name) {
  const m = name?.match(/\.([^.]+)$/);
  return m ? m[1].toLowerCase() : "";
}

function extChipClass(ext) {
  if (["pdf"].includes(ext)) return "ext-chip pdf";
  if (["csv", "tsv"].includes(ext)) return "ext-chip csv";
  if (["zip", "tar", "gz", "7z"].includes(ext)) return "ext-chip zip";
  if (["xml"].includes(ext)) return "ext-chip xml";
  return "ext-chip";
}

function matchesFilter(name, filter) {
  const ext = extOf(name);
  if (filter === "pdf") return ext === "pdf";
  if (filter === "xml") return ext === "xml";
  if (filter === "csv") return ["csv", "tsv"].includes(ext);
  if (filter === "archive") return ["zip", "tar", "gz", "7z"].includes(ext);
  return true;
}

export default function FilesView() {
  const { getToken } = useAuth();
  const { t, lang } = useT();
  const T = t[lang];

  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [prefix, setPrefix] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [sortKey, setSortKey] = useState("name");
  const [sortAsc, setSortAsc] = useState(true);
  const [dragOver, setDragOver] = useState(false);
  const [toast, setToast] = useState(null);
  const [pipelineSteps, setPipelineSteps] = useState([]);
  const fileInput = useRef(null);
  const loadedRef = useRef(false);


  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 3000);
  };

  const loadFiles = useCallback(
    async (pref = prefix) => {
      setLoading(true);
      try {
        const token = await getToken();
        const res = await fetch(
          `/api/files?prefix=${encodeURIComponent(pref)}`,
          {
            headers: { Authorization: `Bearer ${token}` },
          },
        );
        if (!res.ok) throw new Error(res.statusText);
        const data = await res.json();
        setItems(data.items ?? data);
        setPrefix(pref);
      } catch (e) {
        showToast(`Błąd ładowania: ${e.message}`);
      } finally {
        setLoading(false);
      }
    },
    [getToken, prefix],
  );

  useEffect(() => {
    if (!FEATURES.kpi_pipeline) return;
    const fetchPipelineState = async () => {
      try {
        const token = await getToken();
        const now = new Date();
        const res = await fetch(
          `/api/close/state?year=${now.getFullYear()}&month=${now.getMonth() + 1}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (res.ok) {
          const data = await res.json();
          setPipelineSteps(data.completed_steps ?? []);
        }
      } catch {
        // non-blocking
      }
    };
    fetchPipelineState();
  }, [getToken]);

  
  // Load on mount
  if (!loadedRef.current) {
    loadedRef.current = true;
    loadFiles("");
  }

  const deleteFile = async (key) => {
    const name = key.split("/").pop();
    if (!window.confirm(`Usuń plik "${name}"?`)) return;
    try {
      const token = await getToken();
      const res = await fetch(`/api/files/${encodeURIComponent(key)}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(res.statusText);
      showToast(`Usunięto: ${name}`);
      loadFiles(prefix);
    } catch (e) {
      showToast(`Błąd usuwania: ${e.message}`);
    }
  };

  const downloadFile = async (key) => {
    try {
      const token = await getToken();
      const res = await fetch(`/api/files/${encodeURIComponent(key)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(res.statusText);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = key.split("/").pop();
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      showToast(`Błąd pobierania: ${e.message}`);
    }
  };

  const uploadFile = async (file) => {
    const key = prefix ? `${prefix}/${file.name}` : file.name;
    try {
      const token = await getToken();
      const res = await fetch(`/api/files/${encodeURIComponent(key)}`, {
        method: "PUT",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": file.type || "application/octet-stream",
        },
        body: file,
      });
      if (!res.ok) throw new Error(res.statusText);
      showToast(`Wgrano: ${file.name}`);
      loadFiles(prefix);
    } catch (e) {
      showToast(`Błąd wgrywania: ${e.message}`);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  };

  const handleFileInput = (e) => {
    const file = e.target.files[0];
    if (file) uploadFile(file);
    e.target.value = "";
  };

  const toggleSort = (key) => {
    if (sortKey === key) setSortAsc((a) => !a);
    else {
      setSortKey(key);
      setSortAsc(true);
    }
  };

  const segments = prefix ? prefix.split("/") : [];

  const folders = items.filter(
    (i) =>
      i.is_directory ||
      i.type === "folder" ||
      (i.name || i.key || "").endsWith("/"),
  );
  const files = items.filter(
    (i) =>
      !(
        i.is_directory ||
        i.type === "folder" ||
        (i.name || i.key || "").endsWith("/")
      ),
  );

  const getKey = (i) => i.key || i.name || "";
  const getName = (i) => i.name || i.key?.split("/").pop() || "";

  const filtered = files
    .filter((i) => {
      const n = getName(i);
      return (
        matchesFilter(n, filter) &&
        (!search || n.toLowerCase().includes(search.toLowerCase()))
      );
    })
    .sort((a, b) => {
      let va, vb;
      if (sortKey === "name") {
        va = getName(a).toLowerCase();
        vb = getName(b).toLowerCase();
      } else if (sortKey === "size") {
        va = a.size ?? 0;
        vb = b.size ?? 0;
      } else {
        va = a.last_modified ?? "";
        vb = b.last_modified ?? "";
      }
      if (va < vb) return sortAsc ? -1 : 1;
      if (va > vb) return sortAsc ? 1 : -1;
      return 0;
    });

  const SortInd = ({ k }) =>
    sortKey === k ? (
      <span className="sort-ind">{sortAsc ? "↑" : "↓"}</span>
    ) : null;

  return (
    <div
      style={{ display: "flex", flexDirection: "column", gap: "var(--gap)" }}
    >
      <PageHead
        title={T.files_title}
        sub={T.files_sub}
        actions={
          <>
            <input
              ref={fileInput}
              type="file"
              style={{ display: "none" }}
              onChange={handleFileInput}
            />
            <button
              className="btn btn-primary"
              onClick={() => fileInput.current.click()}
            >
              <Icon name="upload" size={14} /> {T.btn_upload}
            </button>
          </>
        }
      />

      {/* KPI row — tylko flagi włączone */}
      {(FEATURES.kpi_files_count ||
        FEATURES.kpi_pipeline ||
        FEATURES.kpi_revenue ||
        FEATURES.kpi_sales_count) && (
        <div className="kpi-row">
          {FEATURES.kpi_revenue && (
            <div className="kpi" style={{ "--kpi-accent": "var(--primary)" }}>
              <div className="kpi-label">{T.kpi_revenue}</div>
              <div className="kpi-value">—</div>
              <div className="kpi-meta">{T.kpi_no_api}</div>
            </div>
          )}
          {FEATURES.kpi_sales_count && (
            <div className="kpi" style={{ "--kpi-accent": "var(--accent)" }}>
              <div className="kpi-label">{T.kpi_sales_count}</div>
              <div className="kpi-value">—</div>
              <div className="kpi-meta">{T.kpi_no_api}</div>
            </div>
          )}
          {FEATURES.kpi_files_count &&
            (() => {
              const allFiles = items.filter(
                (i) =>
                  !(
                    i.is_directory ||
                    i.type === "folder" ||
                    (i.key || i.name || "").endsWith("/")
                  ),
              );
              const today = new Date().toISOString().slice(0, 10);
              const uploadedToday = allFiles.filter((i) =>
                (i.last_modified ?? "").startsWith(today),
              ).length;
              return (
                <div
                  className="kpi"
                  style={{ "--kpi-accent": "var(--success)" }}
                >
                  <div className="kpi-label">{T.kpi_files}</div>
                  <div className="kpi-value">
                    {loading ? "…" : allFiles.length.toLocaleString("pl-PL")}
                  </div>
                  <div className="kpi-meta">
                    {uploadedToday > 0
                      ? `${uploadedToday} ${T.kpi_today}`
                      : T.kpi_files_sub}
                  </div>
                </div>
              );
            })()}
          {FEATURES.kpi_pipeline &&
            (() => {
              const now = new Date();
              const done = pipelineSteps.length;
              const total = PIPELINE_STEPS.length;
              const isReady = done >= total;
              return (
                <div
                  className="kpi"
                  style={{
                    "--kpi-accent": isReady
                      ? "var(--success)"
                      : "var(--warning)",
                  }}
                >
                  <div className="kpi-label">{T.kpi_pipeline}</div>
                  <div className="kpi-value">
                    {isReady ? T.kpi_pipeline_ready : `${done}/${total}`}
                  </div>
                  <div className="kpi-meta">
                    {MONTHS_PL[now.getMonth()]} {now.getFullYear()}
                  </div>
                </div>
              );
            })()}
        </div>
      )}


      {/* Breadcrumbs */}
      <div className="crumbs">
        <Icon name="folder" size={14} className="folder-icon" />
        <button className="seg" onClick={() => loadFiles("")}>
          zdrovena-docs
        </button>
        {segments.map((seg, i) => {
          const p = segments.slice(0, i + 1).join("/");
          return (
            <>
              <span key={`sep-${i}`} className="sep">
                /
              </span>
              <button
                key={`seg-${i}`}
                className={`seg${i === segments.length - 1 ? " current" : ""}`}
                onClick={() => loadFiles(p)}
              >
                {seg}
              </button>
            </>
          );
        })}
      </div>

      {/* Toolbar */}
      <div className="toolbar">
        <div className="search">
          <Icon name="search" size={14} />
          <input
            placeholder={T.search_placeholder}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {search && (
            <button
              className="btn-ghost"
              style={{ padding: "0 4px" }}
              onClick={() => setSearch("")}
            >
              <Icon name="x" size={12} />
            </button>
          )}
        </div>
        <div className="filter-group">
          {[
            ["all", T.filter_all],
            ["pdf", T.filter_pdf],
            ["xml", T.filter_xml],
            ["csv", T.filter_csv],
            ["archive", T.filter_archive],
          ].map(([k, label]) => (
            <button
              key={k}
              className={`filter-btn${filter === k ? " active" : ""}`}
              onClick={() => setFilter(k)}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          className="btn btn-ghost btn-sm"
          onClick={() => loadFiles(prefix)}
          title="Odśwież"
        >
          <Icon
            name="refresh"
            size={14}
            className={loading ? "spinning" : ""}
          />
        </button>
      </div>

      {/* Table */}
      <div className="card">
        <div style={{ overflowX: "auto" }}>
          <table className="files">
            <thead>
              <tr>
                <th className="sortable" onClick={() => toggleSort("name")}>
                  {T.col_name} <SortInd k="name" />
                </th>
                <th
                  className="sortable"
                  onClick={() => toggleSort("size")}
                  style={{ width: 110 }}
                >
                  {T.col_size} <SortInd k="size" />
                </th>
                <th
                  className="sortable"
                  onClick={() => toggleSort("modified")}
                  style={{ width: 160 }}
                >
                  {T.col_modified} <SortInd k="modified" />
                </th>
                <th style={{ width: 90 }} />
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={4} style={{ textAlign: "center", padding: 32 }}>
                    <div className="spinner" style={{ margin: "0 auto" }} />
                  </td>
                </tr>
              )}
              {!loading &&
                folders.map((folder) => {
                  const n = getName(folder);
                  const subPrefix = prefix
                    ? `${prefix}/${n.replace(/\/$/, "")}`
                    : n.replace(/\/$/, "");
                  return (
                    <tr key={getKey(folder)}>
                      <td>
                        <div
                          className="name-cell folder"
                          onClick={() => loadFiles(subPrefix)}
                        >
                          <span className="ext-chip fld">
                            <Icon name="folder" size={12} />
                          </span>
                          <span className="main-text folder-name">
                            {n.replace(/\/$/, "")}
                          </span>
                        </div>
                      </td>
                      <td className="dim">—</td>
                      <td className="dim">—</td>
                      <td />
                    </tr>
                  );
                })}
              {!loading &&
                filtered.map((file) => {
                  const n = getName(file);
                  const ext = extOf(n);
                  return (
                    <tr key={getKey(file)}>
                      <td>
                        <div className="name-cell">
                          <span className={extChipClass(ext)}>
                            {ext.toUpperCase() || "—"}
                          </span>
                          <span className="main-text">{n}</span>
                        </div>
                      </td>
                      <td className="mono">{fmtBytes(file.size)}</td>
                      <td className="mono dim">
                        {fmtDate(file.last_modified)}
                      </td>
                      <td>
                        <div className="row-actions">
                          <button
                            className="icon-btn"
                            title="Pobierz"
                            onClick={() => downloadFile(getKey(file))}
                          >
                            <Icon name="download" size={15} />
                          </button>
                          <button
                            className="icon-btn danger"
                            title="Usuń"
                            onClick={() => deleteFile(getKey(file))}
                          >
                            <Icon name="trash" size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              {!loading && filtered.length === 0 && folders.length === 0 && (
                <tr>
                  <td colSpan={4}>
                    <div className="empty">
                      <div className="h">Brak plików</div>
                      <div>Folder jest pusty lub nie pasuje do filtrów.</div>
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Drop zone */}
        <div
          className={`dropzone${dragOver ? " active" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
        >
          <span className="hint">
            {dragOver ? T.dropzone_active : T.dropzone_hint}
          </span>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => fileInput.current.click()}
          >
            <Icon name="upload" size={13} /> {T.btn_upload}
          </button>
        </div>
      </div>

      {toast && (
        <div className="toast-wrap">
          <div className="toast">
            <span className="dot" />
            {toast}
          </div>
        </div>
      )}
    </div>
  );
}

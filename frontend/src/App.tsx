import { useCallback, useEffect, useMemo, useState } from "react";

type Destination = {
  key: string;
  connector_key: string;
  rules: {
    frequency_cap: number;
    frequency_window_seconds: number;
    self_promo_allowed: boolean;
    link_policy: "allow" | "disallow" | "canonical_only";
    disclosure_required: boolean;
  };
  enabled: boolean;
  last_certified_at: string | null;
  certified_level: string;
};

type Job = {
  id: string;
  destination_key: string;
  status: string;
  progress: number;
  created_at: string;
  result_url: string;
  evidence_url: string;
};

type Certification = {
  id: string;
  destination_key: string;
  status: string;
  cleanup_deadline: string;
  result: unknown;
  error: unknown;
};

type CatalogueEntry = {
  name: string;
  url: string;
  platform_key: string;
  category: string;
  audience_tags: string[];
  connector_key: string;
  level: string;
};

type Content = {
  title: string;
  text: string;
  has_links: boolean;
  canonical_url: string;
  self_promotional: boolean;
  ai_generated: boolean;
  ai_disclosure: boolean;
};

const connectors = ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"];
const navItems = ["Overview", "Destinations", "Publish", "Catalogue", "Insights"] as const;
type Page = (typeof navItems)[number];
const blankContent: Content = {
  title: "",
  text: "",
  has_links: false,
  canonical_url: "",
  self_promotional: false,
  ai_generated: false,
  ai_disclosure: false,
};
const idempotencyKey = () => crypto.randomUUID();
const localJobStorageKey = "incubrix.localJobIds";

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${import.meta.env.VITE_API_BASE_URL ?? ""}${path}`, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const error = payload?.error;
    throw new Error(error?.message ?? `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function App() {
  const [page, setPage] = useState<Page>("Overview");
  const [destinations, setDestinations] = useState<Destination[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [certification, setCertification] = useState<Certification | null>(null);
  const [catalogue, setCatalogue] = useState<CatalogueEntry[]>([]);
  const [alerts, setAlerts] = useState<Record<string, unknown>[]>([]);
  const [health, setHealth] = useState<"checking" | "online" | "offline">("checking");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [content, setContent] = useState<Content>(blankContent);
  const [selectedDestination, setSelectedDestination] = useState("");
  const [destinationKey, setDestinationKey] = useState("");
  const [connectorKey, setConnectorKey] = useState("discourse");
  const [frequencyCap, setFrequencyCap] = useState(1);
  const [linkPolicy, setLinkPolicy] = useState<Destination["rules"]["link_policy"]>("allow");
  const [selfPromoAllowed, setSelfPromoAllowed] = useState(false);
  const [disclosureRequired, setDisclosureRequired] = useState(false);
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [topics, setTopics] = useState("");
  const [toolOutput, setToolOutput] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    try {
      const [destinationList, catalogueResponse, alertList] = await Promise.all([
        api<Destination[]>("/api/v1/destinations"),
        api<{ items: CatalogueEntry[] }>("/api/v1/catalogue?limit=100"),
        api<Record<string, unknown>[]>("/api/v1/alerts?limit=20"),
      ]);
      setDestinations(destinationList);
      setCatalogue(catalogueResponse.items);
      setAlerts(alertList);
      setHealth("online");
      setSelectedDestination((current) =>
        current && destinationList.some((item) => item.key === current)
          ? current
          : destinationList[0]?.key ?? "",
      );
    } catch (cause) {
      setHealth("offline");
      setError(cause instanceof Error ? cause.message : "Could not load application data.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const readLocalJobs = () => {
      try {
        return JSON.parse(localStorage.getItem(localJobStorageKey) ?? "[]") as string[];
      } catch {
        return [];
      }
    };
    const loadJobs = async () => {
      const ids = readLocalJobs();
      const responses = await Promise.allSettled(ids.map((id) => api<Job>(`/api/v1/jobs/${id}`)));
      const found = responses.flatMap((result) =>
        result.status === "fulfilled" ? [result.value] : [],
      );
      setJobs(found.sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 10));
    };
    void loadJobs();
    const timer = window.setInterval(() => void loadJobs(), 4000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!certification || ["succeeded", "failed"].includes(certification.status)) return;
    let active = true;
    const loadCertification = async () => {
      try {
        const current = await api<Certification>(`/api/v1/certifications/${certification.id}`);
        if (!active) return;
        setCertification(current);
        if (["succeeded", "failed"].includes(current.status)) await refresh();
      } catch {
        if (active) setError("Could not retrieve certification status.");
      }
    };
    const timer = window.setInterval(() => void loadCertification(), 2500);
    void loadCertification();
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [certification?.id, certification?.status, refresh]);

  const completedJobs = useMemo(
    () => jobs.filter((job) => job.status === "succeeded").length,
    [jobs],
  );

  const runAction = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The request failed.");
    } finally {
      setBusy(false);
    }
  };

  const updateContent = (field: keyof Content, value: string | boolean) =>
    setContent((current) => ({ ...current, [field]: value }));

  const submitJob = () =>
    runAction(async () => {
      if (!selectedDestination) throw new Error("Create or select a destination first.");
      if (!content.text.trim()) throw new Error("Add publication content before submitting.");
      const job = await api<Job>("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey() },
        body: JSON.stringify({
          destination_key: selectedDestination,
          content: { ...content, canonical_url: content.canonical_url || null },
        }),
      });
      const ids = JSON.parse(localStorage.getItem(localJobStorageKey) ?? "[]") as string[];
      localStorage.setItem(localJobStorageKey, JSON.stringify([job.id, ...ids.filter((id) => id !== job.id)].slice(0, 25)));
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)].slice(0, 10));
      setNotice(`Publication job ${job.id.slice(0, 8)} accepted. It will appear in recent jobs.`);
      setPage("Overview");
    });

  const createDestination = () =>
    runAction(async () => {
      if (!destinationKey.trim()) throw new Error("Enter a destination key.");
      const destination = await api<Destination>("/api/v1/destinations", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey() },
        body: JSON.stringify({
          key: destinationKey.trim(),
          connector_key: connectorKey,
          config: {},
          rules: {
            frequency_cap: Number(frequencyCap),
            frequency_window_seconds: 604800,
            self_promo_allowed: selfPromoAllowed,
            link_policy: linkPolicy,
            disclosure_required: disclosureRequired,
          },
        }),
      });
      setDestinations((current) => [...current, destination]);
      setSelectedDestination(destination.key);
      setDestinationKey("");
      setNotice(`Destination "${destination.key}" created. Connector execution remains simulation-first.`);
    });

  const importCatalogue = () =>
    runAction(async () => {
      if (!csvFile) throw new Error("Choose a CSV file to import.");
      const csv = await csvFile.text();
      const report = await api<Record<string, unknown>>("/api/v1/catalogue/import", {
        method: "POST",
        headers: { "Content-Type": "text/csv", "Idempotency-Key": idempotencyKey() },
        body: csv,
      });
      setToolOutput(report);
      setCsvFile(null);
      await refresh();
      setNotice("Catalogue import processed. Review the row-level report below.");
    });

  const downloadCatalogue = async () => {
    setError("");
    try {
      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL ?? ""}/api/v1/catalogue/export`);
      if (!response.ok) throw new Error(`Export failed (${response.status}).`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "destinations.csv";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Catalogue export failed.");
    }
  };

  const runContentTool = (path: "/api/v1/tailor" | "/api/v1/recommendations") =>
    runAction(async () => {
      if (!content.text.trim()) throw new Error("Add content in the Publish workspace first.");
      const payload =
        path === "/api/v1/tailor"
          ? {
              content: { ...content, canonical_url: content.canonical_url || null },
              destination_keys: destinations.map((destination) => destination.key),
              limit: 10,
            }
          : {
              content: { ...content, canonical_url: content.canonical_url || null },
              topics: topics.split(",").map((topic) => topic.trim()).filter(Boolean),
              limit: 10,
            };
      const result = await api<unknown>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey() },
        body: JSON.stringify(payload),
      });
      setToolOutput(result);
      setNotice(path.endsWith("tailor") ? "Tailoring suggestions are ready for review." : "Destination recommendations are ready.");
    });

  const certify = (key: string) =>
    runAction(async () => {
      const started = await api<Certification>(`/api/v1/destinations/${encodeURIComponent(key)}/certify`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey() },
      });
      setCertification(started);
      setNotice(`Certification run ${started.id.slice(0, 8)} started. Check back for the evidence result.`);
    });

  const contentEditor = (
    <div className="content-editor">
      <label className="field">
        <span>Title</span>
        <input value={content.title} onChange={(event) => updateContent("title", event.target.value)} placeholder="A clear, useful title" />
      </label>
      <label className="field">
        <span>Content</span>
        <textarea value={content.text} onChange={(event) => updateContent("text", event.target.value)} rows={7} placeholder="Write or paste the content you want to distribute." />
      </label>
      <div className="field-grid">
        <label className="field">
          <span>Canonical URL <small>Optional</small></span>
          <input value={content.canonical_url} onChange={(event) => updateContent("canonical_url", event.target.value)} placeholder="https://example.com/original" />
        </label>
        <label className="field">
          <span>Target destination</span>
          <select value={selectedDestination} onChange={(event) => setSelectedDestination(event.target.value)}>
            <option value="">Select a destination</option>
            {destinations.map((destination) => <option key={destination.key} value={destination.key}>{destination.key} · {destination.connector_key}</option>)}
          </select>
        </label>
      </div>
      <div className="check-grid">
        <Check label="Contains links" checked={content.has_links} onChange={(value) => updateContent("has_links", value)} />
        <Check label="Self-promotional" checked={content.self_promotional} onChange={(value) => updateContent("self_promotional", value)} />
        <Check label="AI-generated" checked={content.ai_generated} onChange={(value) => updateContent("ai_generated", value)} />
        <Check label="AI disclosure included" checked={content.ai_disclosure} onChange={(value) => updateContent("ai_disclosure", value)} />
      </div>
      <p className="helper">Destination rules are checked by the API before accepting and again before connector publication.</p>
    </div>
  );

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#" onClick={(event) => { event.preventDefault(); setPage("Overview"); }}>
          <span className="brand-mark">i</span>
          <span>incubrix<span className="brand-light">distribution</span></span>
        </a>
        <div className="workspace-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {navItems.map((item, index) => (
            <button className={`nav-link ${page === item ? "active" : ""}`} key={item} onClick={() => setPage(item)}>
              <span className="nav-icon">{["◫", "◎", "↗", "▤", "⌁"][index]}</span>{item}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="status-card">
            <span className={`status-dot ${health}`} />
            <div><strong>API {health}</strong><small>Local application status</small></div>
          </div>
          <div className="account"><span className="avatar">IB</span><div><strong>IncuBrix</strong><small>Distribution workspace</small></div><span className="more">···</span></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> <strong>{page}</strong></div>
          <div className="topbar-right"><span className="simulation-badge"><i /> Simulation-first</span><button className="icon-button" aria-label="Refresh data" onClick={() => void refresh()}>↻</button></div>
        </header>
        <div className="page-wrap">
          {(notice || error) && <div className={`notice ${error ? "notice-error" : ""}`} role="status">{error || notice}<button onClick={() => { setError(""); setNotice(""); }} aria-label="Dismiss">×</button></div>}

          {page === "Overview" && (
            <>
              <div className="page-heading">
                <div><p className="eyebrow">DISTRIBUTION CONTROL CENTER</p><h1>Good publishing starts here.</h1><p className="subheading">Manage your destinations and keep every publication workflow in view.</p></div>
                <button className="button button-primary" onClick={() => setPage("Publish")}>＋ New publication</button>
              </div>
              <section className="metric-grid">
                <Metric label="Destinations" value={destinations.length} caption="Configured in this workspace" icon="◎" />
                <Metric label="Catalogue entries" value={catalogue.length} caption="Imported directory records" icon="▤" />
                <Metric label="Completed jobs" value={completedJobs} caption="Visible from this browser" icon="↗" />
                <Metric label="Open alerts" value={alerts.length} caption="Requiring attention" icon="!" />
              </section>
              <div className="section-heading"><div><h2>Recent publications</h2><p>Jobs submitted from this browser are tracked here.</p></div><button className="text-button" onClick={() => setPage("Publish")}>Create a job <span>→</span></button></div>
              <div className="panel">
                {jobs.length ? <div className="job-list">{jobs.map((job) => <JobRow key={job.id} job={job} />)}</div> : <EmptyState title="No publication jobs yet" detail="Start with a destination and submit your first piece of content." action="Set up a destination" onClick={() => setPage("Destinations")} />}
              </div>
              <div className="info-banner"><span className="info-icon">i</span><div><strong>Simulation-first by design</strong><p>Connector requests run in simulation by default. No external platform is contacted unless you explicitly configure and approve a real target.</p></div></div>
            </>
          )}

          {page === "Destinations" && (
            <>
              <div className="page-heading"><div><p className="eyebrow">NETWORK</p><h1>Destinations</h1><p className="subheading">Configure connector targets and enforce each community's posting rules.</p></div></div>
              <div className="two-column-layout">
                <section className="panel form-panel">
                  <div className="panel-heading"><div><h2>Add destination</h2><p>Use a unique key to identify the target.</p></div><span className="step-number">01</span></div>
                  <form onSubmit={(event) => { event.preventDefault(); void createDestination(); }}>
                    <label className="field"><span>Destination key</span><input value={destinationKey} onChange={(event) => setDestinationKey(event.target.value)} placeholder="e.g. product-community" maxLength={64} /></label>
                    <label className="field"><span>Connector</span><select value={connectorKey} onChange={(event) => setConnectorKey(event.target.value)}>{connectors.map((connector) => <option key={connector}>{connector}</option>)}</select></label>
                    <label className="field"><span>Posting frequency cap <small>per 7-day window</small></span><input type="number" min={1} max={1000} value={frequencyCap} onChange={(event) => setFrequencyCap(Number(event.target.value))} /></label>
                    <label className="field"><span>Link policy</span><select value={linkPolicy} onChange={(event) => setLinkPolicy(event.target.value as Destination["rules"]["link_policy"])}><option value="allow">Allow links</option><option value="canonical_only">Canonical link only</option><option value="disallow">Disallow links</option></select></label>
                    <div className="form-checks"><Check label="Self-promotion allowed" checked={selfPromoAllowed} onChange={setSelfPromoAllowed} /><Check label="AI disclosure required" checked={disclosureRequired} onChange={setDisclosureRequired} /></div>
                    <button className="button button-primary full-width" disabled={busy}>{busy ? "Saving…" : "Add destination"}</button>
                  </form>
                </section>
                <section className="panel">
                  <div className="panel-heading"><div><h2>Configured network</h2><p>{destinations.length} destination{destinations.length === 1 ? "" : "s"} registered</p></div><span className="count-pill">{destinations.length}</span></div>
                  {destinations.length ? <div className="destination-list">{destinations.map((destination) => <DestinationCard key={destination.key} destination={destination} onCertify={() => void certify(destination.key)} busy={busy} />)}</div> : <EmptyState title="Your network is empty" detail="Add a destination to configure the connector and posting rules." />}
                </section>
              </div>
              {certification && <OutputPanel title={`Certification · ${certification.destination_key} · ${certification.status}`} value={certification} />}
            </>
          )}

          {page === "Publish" && (
            <>
              <div className="page-heading"><div><p className="eyebrow">CONTENT WORKFLOW</p><h1>Prepare a publication</h1><p className="subheading">Compose once, validate against destination rules, and track the result.</p></div></div>
              <section className="panel compose-panel">
                <div className="panel-heading"><div><h2>Content and destination</h2><p>Required disclosure and link rules are applied server-side.</p></div><span className="step-number">01</span></div>
                {contentEditor}
                <div className="compose-actions"><button className="button button-outline" disabled={busy} onClick={() => void runContentTool("/api/v1/tailor")}>Suggest tailored versions</button><button className="button button-primary" disabled={busy || !selectedDestination || !content.text.trim()} onClick={() => void submitJob()}>{busy ? "Submitting…" : "Submit publication →"}</button></div>
              </section>
            </>
          )}

          {page === "Catalogue" && (
            <>
              <div className="page-heading"><div><p className="eyebrow">DESTINATION DIRECTORY</p><h1>Catalogue</h1><p className="subheading">Import a prepared CSV, inspect validated entries, and export the current directory.</p></div><button className="button button-outline" onClick={() => void downloadCatalogue()}>↓ Export CSV</button></div>
              <section className="panel import-panel">
                <div><h2>Import destination records</h2><p>CSV rows are validated and deduplicated by normalized URL and platform. URLs are not fetched or scraped.</p></div>
                <div className="import-controls"><label className="file-picker"><input type="file" accept=".csv,text/csv" onChange={(event) => setCsvFile(event.target.files?.[0] ?? null)} /><span>＋ Choose CSV</span></label><span className="file-name">{csvFile?.name ?? "No file selected"}</span><button className="button button-primary" disabled={!csvFile || busy} onClick={() => void importCatalogue()}>{busy ? "Importing…" : "Import catalogue"}</button></div>
              </section>
              {toolOutput && <OutputPanel title="Latest import report" value={toolOutput} />}
              <section className="panel table-panel">
                <div className="panel-heading"><div><h2>Catalogue entries</h2><p>{catalogue.length} shown from the latest page</p></div><button className="icon-button" onClick={() => void refresh()} aria-label="Refresh catalogue">↻</button></div>
                {catalogue.length ? <div className="table-scroll"><table><thead><tr><th>Destination</th><th>Platform</th><th>Category</th><th>Audience</th><th>Level</th></tr></thead><tbody>{catalogue.map((entry) => <tr key={`${entry.platform_key}-${entry.url}`}><td><strong>{entry.name}</strong><a href={entry.url} target="_blank" rel="noreferrer">{entry.url}</a></td><td><span className="tag">{entry.platform_key}</span></td><td>{entry.category}</td><td>{entry.audience_tags.join(", ")}</td><td><span className="level">{entry.level}</span></td></tr>)}</tbody></table></div> : <EmptyState title="No catalogue entries" detail="Import records using the assessment's destination catalogue schema." />}
              </section>
            </>
          )}

          {page === "Insights" && (
            <>
              <div className="page-heading"><div><p className="eyebrow">DISCOVERY & HEALTH</p><h1>Insights</h1><p className="subheading">Get deterministic recommendations, tailoring suggestions, and a view of current alerts.</p></div></div>
              <section className="panel insights-tool">
                <div className="panel-heading"><div><h2>Find a suitable destination</h2><p>Enter comma-separated topics to rank catalogue records against the content.</p></div><span className="step-number">01</span></div>
                {contentEditor}
                <label className="field topics-field"><span>Topics</span><input value={topics} onChange={(event) => setTopics(event.target.value)} placeholder="e.g. developer tools, open source, education" /></label>
                <div className="compose-actions"><button className="button button-outline" disabled={busy} onClick={() => void runContentTool("/api/v1/tailor")}>Tailor for destinations</button><button className="button button-primary" disabled={busy || !topics.trim()} onClick={() => void runContentTool("/api/v1/recommendations")}>Get recommendations →</button></div>
              </section>
              {toolOutput && <OutputPanel title="Latest insight result" value={toolOutput} />}
              <section className="panel alerts-panel"><div className="panel-heading"><div><h2>Open alerts</h2><p>Alerts returned by the API; verify target status before taking action.</p></div><span className="count-pill">{alerts.length}</span></div>{alerts.length ? <div className="alert-list">{alerts.map((alert, index) => <div className="alert-row" key={String(alert.id ?? index)}><span className="alert-symbol">!</span><div><strong>{String(alert.error_code ?? "Alert")}</strong><p>{String(alert.message ?? "Review this destination.")}</p></div><span className="tag">{String(alert.destination_key ?? "—")}</span></div>)}</div> : <div className="empty-inline">No open alerts right now.</div>}</section>
            </>
          )}
        </div>
        <footer className="footer"><span>IncuBrix Distribution</span><span>Simulation-first · Review destination rules before publishing</span></footer>
      </main>
    </div>
  );
}

function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return <label className="check-label"><input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} /><span className="custom-check" />{label}</label>;
}

function Metric({ label, value, caption, icon }: { label: string; value: number; caption: string; icon: string }) {
  return <article className="metric-card"><div className="metric-top"><span>{label}</span><i>{icon}</i></div><strong>{value.toLocaleString()}</strong><small>{caption}</small></article>;
}

function JobRow({ job }: { job: Job }) {
  const state = job.status.toLowerCase();
  return <div className="job-row"><span className={`job-status status-${state}`}>{state}</span><div className="job-main"><strong>{job.destination_key}</strong><small>Job {job.id.slice(0, 8)} · {new Date(job.created_at).toLocaleString()}</small><div className="progress-track"><span style={{ width: `${job.progress}%` }} /></div></div><span className="progress-label">{job.progress}%</span></div>;
}

function DestinationCard({ destination, onCertify, busy }: { destination: Destination; onCertify: () => void; busy: boolean }) {
  return <article className="destination-card"><div className="destination-card-top"><span className="connector-icon">{destination.connector_key.slice(0, 2).toUpperCase()}</span><div className="destination-main"><strong>{destination.key}</strong><small>{destination.connector_key} connector</small></div><span className={`enabled-pill ${destination.enabled ? "" : "disabled-pill"}`}>{destination.enabled ? "Enabled" : "Disabled"}</span></div><div className="rule-tags"><span>Cap {destination.rules.frequency_cap} / {Math.round(destination.rules.frequency_window_seconds / 86400)}d</span><span>{destination.rules.link_policy.replace("_", " ")}</span><span>{destination.rules.self_promo_allowed ? "Promo allowed" : "No promotion"}</span><span>{destination.rules.disclosure_required ? "Disclosure required" : "Disclosure optional"}</span></div><div className="destination-card-bottom"><span className="cert-level">Certification {destination.certified_level}{destination.last_certified_at ? ` · ${new Date(destination.last_certified_at).toLocaleDateString()}` : " · not certified"}</span><button className="text-button small-button" disabled={busy || !destination.enabled} onClick={onCertify}>Run checks →</button></div></article>;
}

function EmptyState({ title, detail, action, onClick }: { title: string; detail: string; action?: string; onClick?: () => void }) {
  return <div className="empty-state"><span className="empty-icon">◌</span><strong>{title}</strong><p>{detail}</p>{action && onClick && <button className="button button-outline" onClick={onClick}>{action}</button>}</div>;
}

function OutputPanel({ title, value }: { title: string; value: unknown }) {
  return <section className="panel output-panel"><div className="panel-heading"><div><h2>{title}</h2><p>Review this response before using it.</p></div></div><pre>{JSON.stringify(value, null, 2)}</pre></section>;
}

export default App;

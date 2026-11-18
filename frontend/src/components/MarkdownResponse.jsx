import React, { useRef, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { ExternalLink, FileText, Loader2, Maximize2, Minimize2, Search, X } from "lucide-react";

import { api } from "../api/client.js";
import { useModalDismiss } from "../hooks/useModalDismiss.js";

// Content-library documents whose chunks are enacted law, so a citation to one
// offers secondary sources rather than more statutes.
const STATUTE_SLUGS = new Set(["ohio-revised-code", "ohio-administrative-code", "federal-housing-law"]);

function citationMap(citations) {
  return new Map((citations || []).map((citation, index) => [String(index + 1), citation]));
}

function addCitationLinks(content, citations) {
  const knownCitations = citationMap(citations);
  return String(content || "").replace(/\[(\d+)\]/g, (match, number) => (
    knownCitations.has(number) ? `[${number}](#citation-${number})` : match
  ));
}

export function isCaseLawCitation(citation) {
  return citation?.sourceKind === "local_cases" && citation?.metadata?.decisionId;
}

export function isContentLibraryCitation(citation) {
  return citation?.sourceKind === "rag" && citation?.metadata?.documentSlug && citation?.metadata?.chunkId;
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.valueOf())) return value;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function MetadataRow({ label, value }) {
  if (value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>{Array.isArray(value) ? value.join("; ") : value}</dd>
    </div>
  );
}

function MetadataList({ title, items }) {
  const visible = (items || []).filter(Boolean);
  if (!visible.length) return null;
  return (
    <section className="case-source-section">
      <h5>{title}</h5>
      <ul>
        {visible.map((item, index) => <li key={`${title}-${index}`}>{item}</li>)}
      </ul>
    </section>
  );
}

function relatedActionsFor(citation, current) {
  if (!citation) return [];
  if (isCaseLawCitation(citation)) {
    const caseQuery = citation.citation || citation.title;
    const statutes = [
      ...(current?.statutesCited || []),
      ...(current?.regulationsCited || []),
    ].join(" ");
    return [
      { label: "Secondary sources mentioning this case", payload: { query: caseQuery, sourceMode: "manual", sourceKinds: ["rag"], sourceIds: ["treatise", "hud-handbook", "green-book"], useAi: false } },
      ...(statutes ? [{ label: "Statutes and regulations cited", payload: { query: statutes, sourceMode: "manual", sourceKinds: ["rag"], sourceIds: ["ohio-statutes", "federal-housing-law"], useAi: false } }] : []),
    ];
  }
  if (isContentLibraryCitation(citation)) {
    const citationText = current?.citation || citation.citation || current?.heading || citation.title;
    const sourceIds = STATUTE_SLUGS.has(current?.documentSlug)
      ? ["treatise", "hud-handbook", "green-book"]
      : ["ohio-cases"];
    const label = STATUTE_SLUGS.has(current?.documentSlug)
      ? "Secondary sources discussing this statute"
      : "Cases mentioning this section";
    return [
      { label, payload: { query: citationText, sourceMode: "manual", sourceKinds: STATUTE_SLUGS.has(current?.documentSlug) ? ["rag"] : ["local_cases"], sourceIds, useAi: false } },
      ...(!STATUTE_SLUGS.has(current?.documentSlug) ? [{ label: "Statutes related to this section", payload: { query: `${citationText} ${citation.snippet || ""}`, sourceMode: "manual", sourceKinds: ["rag"], sourceIds: ["ohio-statutes", "federal-housing-law"], useAi: false } }] : []),
    ];
  }
  return [];
}

function RelatedSources({ citation, current, onOpenSource }) {
  const [busy, setBusy] = useState("");
  const [results, setResults] = useState([]);
  const [error, setError] = useState("");
  const actions = relatedActionsFor(citation, current);
  if (!actions.length) return null;

  async function runRelated(action) {
    setBusy(action.label);
    setError("");
    try {
      const response = await api.research({ ...action.payload, limitPerSource: 4 });
      setResults(response.results || []);
    } catch (err) {
      setError(err.message || "Could not load related sources.");
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="case-source-section related-source-section">
      <h5>Related sources</h5>
      <div className="related-source-actions">
        {actions.map((action) => (
          <button key={action.label} className="text-link-button" type="button" disabled={!!busy} onClick={() => runRelated(action)}>
            {busy === action.label ? <Loader2 className="spin" size={14} /> : <FileText size={14} />}
            {action.label}
          </button>
        ))}
      </div>
      {error && <div className="inline-error">{error}</div>}
      {results.length > 0 && (
        <div className="related-source-results">
          {results.map((result) => (
            <article key={result.id}>
              <strong>{result.title}</strong>
              <p>{result.snippet}</p>
              <small>{result.sourceLabel}{result.citation ? ` · ${result.citation}` : ""}</small>
              <SourceFullViewButton citation={result} onOpen={onOpenSource} />
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

export function CaseFacetBrowser({ decisionId = "", initialQuery = "", onOpenSource, compact = false }) {
  const [open, setOpen] = useState(!compact);
  const [query, setQuery] = useState(initialQuery || "");
  const [facet, setFacet] = useState({ facet: "", value: "" });
  const [payload, setPayload] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load(next = {}) {
    const nextQuery = next.query ?? query;
    const nextFacet = next.facetObject ?? facet;
    setBusy(true);
    setError("");
    try {
      const response = await api.caselawBrowse({
        q: nextQuery,
        decisionId,
        facet: nextFacet.facet,
        value: nextFacet.value,
      });
      setPayload(response);
      setOpen(true);
    } catch (err) {
      setError(err.message || "Could not browse related cases.");
    } finally {
      setBusy(false);
    }
  }

  React.useEffect(() => {
    if (!compact || decisionId) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decisionId]);

  const facetGroups = payload?.facets || {};
  const clusters = payload?.clusters || [];
  const fallbackResults = payload?.results || [];

  return (
    <section className={`case-facet-browser ${compact ? "compact" : ""}`}>
      <div className="case-facet-heading">
        <div>
          <h5>{decisionId ? "Related case clusters" : "Case facet browser"}</h5>
          {payload && <small>{payload.totalCandidates} candidate case{payload.totalCandidates === 1 ? "" : "s"}</small>}
        </div>
        {compact && (
          <button className="text-link-button" type="button" disabled={busy} onClick={() => open ? setOpen(false) : load()}>
            {busy ? <Loader2 className="spin" size={14} /> : <Search size={14} />}
            {open ? "Hide cases" : "Expand to more cases"}
          </button>
        )}
      </div>
      {open && (
        <>
          {!decisionId && (
            <form className="case-facet-search" onSubmit={(event) => { event.preventDefault(); load({ query }); }}>
              <input className="form-control" value={query} placeholder="Search imported cases" onChange={(event) => setQuery(event.target.value)} />
              <button className="btn btn-light" type="submit" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : <Search size={14} />}</button>
            </form>
          )}
          {facet.value && (
            <button className="text-link-button" type="button" onClick={() => { const cleared = { facet: "", value: "" }; setFacet(cleared); load({ facetObject: cleared }); }}>
              Clear facet: {facet.value}
            </button>
          )}
          {error && <div className="inline-error">{error}</div>}
          {Object.entries(facetGroups).some(([, items]) => items.length > 0) && (
            <div className="case-facet-chip-grid">
              {Object.entries(facetGroups).map(([facetName, items]) => items.slice(0, 8).map((item) => (
                <button
                  key={`${facetName}-${item.value}`}
                  type="button"
                  className="case-facet-chip"
                  onClick={() => { setFacet({ facet: item.facet, value: item.value }); load({ facetObject: { facet: item.facet, value: item.value } }); }}
                >
                  <span>{item.value}</span>
                  <small>{item.count}</small>
                </button>
              )))}
            </div>
          )}
          <div className="case-cluster-list">
            {(clusters.length ? clusters : [{ label: "Cases", results: fallbackResults.slice(0, 12) }]).map((cluster) => (
              <section key={cluster.label} className="case-cluster">
                <h6>{cluster.label}</h6>
                {cluster.results.map((result) => (
                  <article key={result.id}>
                    <strong>{result.title}</strong>
                    <p>{result.snippet}</p>
                    <small>{result.citation}{result.clusterReasons?.length ? ` · ${result.clusterReasons.join(", ")}` : ""}</small>
                    <SourceFullViewButton citation={result} onOpen={onOpenSource} />
                  </article>
                ))}
              </section>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export function CaseLawSourceModal({ citation, onClose, onOpenSource = () => {} }) {
  const [decision, setDecision] = useState(null);
  const [error, setError] = useState("");
  const [readingMode, setReadingMode] = useState(false);
  const decisionId = citation?.metadata?.decisionId;
  const dialogRef = useRef(null);
  useModalDismiss(dialogRef, onClose, { active: Boolean(citation && decisionId) });

  React.useEffect(() => {
    let cancelled = false;
    setDecision(null);
    setError("");
    if (!decisionId) return () => {};
    api.caselawDecision(decisionId)
      .then((response) => {
        if (!cancelled) setDecision(response.decision);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Could not load decision metadata.");
      });
    return () => { cancelled = true; };
  }, [decisionId]);

  if (!citation || !decisionId) return null;
  const current = decision || {
    title: citation.title,
    citation: citation.citation,
    court: citation.metadata?.court,
    county: citation.metadata?.county,
    judge: citation.metadata?.judge,
    decisionDate: citation.metadata?.decisionDate,
    publicationStatus: citation.metadata?.publicationStatus,
    precedentialStatus: citation.metadata?.precedentialStatus,
    authorityLevel: citation.metadata?.authorityLevel,
    treatmentStatus: citation.metadata?.treatmentStatus,
    metadataVerified: citation.metadata?.metadataVerified,
    approvedForDrafting: citation.metadata?.approvedForDrafting,
  };

  return (
    <div className="modal-backdrop case-source-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className={`editor-modal case-source-modal ${readingMode ? "reading-mode" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="case-source-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-heading">
          <div>
            <span className="block-kicker">Local case law</span>
            <h4 id="case-source-title">{current.title || "Case-law decision"}</h4>
          </div>
          <div className="modal-heading-actions">
            <button
              className="btn btn-light icon-button"
              type="button"
              aria-label={readingMode ? "Show metadata sidebar" : "Read PDF full screen"}
              title={readingMode ? "Show metadata sidebar" : "Read PDF full screen"}
              onClick={() => setReadingMode((value) => !value)}
            >
              {readingMode ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
            </button>
            <button className="btn btn-light icon-button" type="button" aria-label="Close source preview" onClick={onClose}>
              <X size={18} />
            </button>
          </div>
        </div>
        <div className={`case-source-layout ${readingMode ? "reading-mode" : ""}`}>
          <aside className="case-source-sidebar">
            {error && <div className="inline-error">{error}</div>}
            {!decision && !error && (
              <div className="case-source-loading"><Loader2 className="spin" size={16} /> Loading metadata</div>
            )}
            <dl className="case-source-metadata">
              <MetadataRow label="Citation" value={current.citation || citation.citation} />
              <MetadataRow label="Docket" value={current.docketNumber || citation.metadata?.docketNumber} />
              <MetadataRow label="Court" value={current.court} />
              <MetadataRow label="County" value={current.county} />
              <MetadataRow label="Judge" value={current.judge} />
              <MetadataRow label="Decision date" value={formatDate(current.decisionDate)} />
              <MetadataRow label="Entry date" value={formatDate(current.entryDate)} />
              <MetadataRow label="Authority" value={current.authorityLevel} />
              <MetadataRow label="Publication" value={current.publicationStatus} />
              <MetadataRow label="Precedential status" value={current.precedentialStatus} />
              <MetadataRow label="Treatment" value={current.treatmentStatus} />
              <MetadataRow label="Verified metadata" value={current.metadataVerified ? "Yes" : "No"} />
              <MetadataRow label="Approved for drafting" value={current.approvedForDrafting ? "Yes" : "No"} />
            </dl>
            {current.treatmentStatus === "unchecked" && (
              <p className="case-source-warning">Treatment and currentness have not been checked.</p>
            )}
            {current.keyFacts && (
              <section className="case-source-section">
                <h5>Key facts</h5>
                <p>{current.keyFacts}</p>
              </section>
            )}
            {current.outcome && (
              <section className="case-source-section">
                <h5>Outcome</h5>
                <p>{current.outcome}</p>
              </section>
            )}
            <MetadataList title="Issues" items={current.issues} />
            <MetadataList title="Holdings" items={current.holdings} />
            <MetadataList title="Rules and authorities" items={[...(current.rulesApplied || []), ...(current.statutesCited || []), ...(current.regulationsCited || []), ...(current.casesCited || [])]} />
            <CaseFacetBrowser decisionId={decisionId} onOpenSource={onOpenSource} />
            <RelatedSources citation={citation} current={current} onOpenSource={onOpenSource} />
          </aside>
          <div className="case-source-pdf">
            <iframe
              title={`${current.title || "Case-law decision"} PDF`}
              src={api.caselawDecisionPdfUrl(decisionId)}
            />
          </div>
        </div>
      </section>
    </div>
  );
}

export function ContentLibrarySourceModal({ citation, onClose, onOpenSource }) {
  const [source, setSource] = useState(null);
  const [error, setError] = useState("");
  const [readingMode, setReadingMode] = useState(false);
  const documentSlug = citation?.metadata?.documentSlug;
  const chunkId = citation?.metadata?.chunkId;
  const dialogRef = useRef(null);
  useModalDismiss(dialogRef, onClose, { active: Boolean(citation && documentSlug && chunkId) });

  React.useEffect(() => {
    let cancelled = false;
    setSource(null);
    setError("");
    if (!documentSlug || !chunkId) return () => {};
    api.contentSource(documentSlug, chunkId)
      .then((response) => {
        if (!cancelled) setSource(response.source);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Could not load source metadata.");
      });
    return () => { cancelled = true; };
  }, [documentSlug, chunkId]);

  if (!citation || !documentSlug || !chunkId) return null;
  const current = source || {
    documentTitle: citation.title,
    documentSlug,
    chunkId,
    heading: citation.title,
    citation: citation.citation,
    sectionPath: citation.metadata?.sectionPath || [],
    pdfPages: citation.metadata?.pdfPages || [],
    sourcePath: citation.metadata?.sourcePath,
    jurisdiction: citation.metadata?.jurisdiction,
    effectiveDate: citation.metadata?.effectiveDate,
    sourceText: citation.snippet,
    hasPdf: Boolean(citation.metadata?.sourcePath),
  };
  const page = current.pdfPages?.[0];
  const isStatute = STATUTE_SLUGS.has(current.documentSlug);

  return (
    <div className="modal-backdrop case-source-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className={`editor-modal case-source-modal ${readingMode ? "reading-mode" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="content-source-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-heading">
          <div>
            <span className="block-kicker">{isStatute ? "Statute" : "Secondary source"}</span>
            <h4 id="content-source-title">{current.documentTitle || citation.title}</h4>
          </div>
          <div className="modal-heading-actions">
            {current.hasPdf && (
              <button
                className="btn btn-light icon-button"
                type="button"
                aria-label={readingMode ? "Show metadata sidebar" : "Read PDF full screen"}
                title={readingMode ? "Show metadata sidebar" : "Read PDF full screen"}
                onClick={() => setReadingMode((value) => !value)}
              >
                {readingMode ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
              </button>
            )}
            <button className="btn btn-light icon-button" type="button" aria-label="Close source preview" onClick={onClose}>
              <X size={18} />
            </button>
          </div>
        </div>
        <div className={`case-source-layout ${readingMode ? "reading-mode" : ""}`}>
          <aside className="case-source-sidebar">
            {error && <div className="inline-error">{error}</div>}
            {!source && !error && (
              <div className="case-source-loading"><Loader2 className="spin" size={16} /> Loading source</div>
            )}
            <dl className="case-source-metadata">
              <MetadataRow label="Citation" value={current.citation || citation.citation} />
              <MetadataRow label="Section" value={current.heading} />
              <MetadataRow label="Path" value={current.sectionPath?.join(" > ")} />
              <MetadataRow label="PDF page" value={page ? String(page) : ""} />
              <MetadataRow label="Version" value={current.documentVersion} />
              <MetadataRow label="Jurisdiction" value={current.jurisdiction} />
              <MetadataRow label="Effective date" value={current.effectiveDate} />
              <MetadataRow label="Source path" value={current.sourcePath} />
            </dl>
            <section className="case-source-section">
              <h5>Quoted text</h5>
              <p>{current.sourceText || citation.snippet}</p>
            </section>
            <RelatedSources citation={citation} current={current} onOpenSource={onOpenSource} />
          </aside>
          <div className={`case-source-pdf ${current.hasPdf ? "" : "text-viewer"}`}>
            {current.hasPdf ? (
              <iframe
                title={`${current.documentTitle || "Source"} PDF`}
                src={api.contentSourcePdfUrl(documentSlug, chunkId, page)}
              />
            ) : (
              <article className="internal-source-text">
                <span className="block-kicker">{current.citation || current.heading}</span>
                <h3>{current.heading}</h3>
                <pre>{current.sourceText || citation.snippet}</pre>
                {current.url && <a href={current.url} target="_blank" rel="noreferrer">Official source <ExternalLink size={14} /></a>}
              </article>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

export function SourceBrowserModal({ citation, onClose }) {
  const [nestedCitation, setNestedCitation] = useState(null);
  if (!citation) return null;
  return (
    <>
      {isCaseLawCitation(citation) && (
        <CaseLawSourceModal citation={citation} onClose={onClose} onOpenSource={setNestedCitation} />
      )}
      {isContentLibraryCitation(citation) && (
        <ContentLibrarySourceModal citation={citation} onClose={onClose} onOpenSource={setNestedCitation} />
      )}
      <SourceBrowserModal citation={nestedCitation} onClose={() => setNestedCitation(null)} />
    </>
  );
}

export function SourceFullViewButton({ citation, onOpen }) {
  if (isCaseLawCitation(citation) || isContentLibraryCitation(citation)) {
    return (
      <button className="text-link-button" type="button" onClick={() => onOpen(citation)}>
        <FileText size={14} /> View full source
      </button>
    );
  }
  if (!citation?.url) return null;
  return (
    <a href={citation.url} target="_blank" rel="noreferrer">
      View full source <ExternalLink size={14} />
    </a>
  );
}

export function CitationPreviewModal({ citation, onClose }) {
  const [caseSource, setCaseSource] = useState(null);
  const dialogRef = useRef(null);
  useModalDismiss(dialogRef, onClose, { active: Boolean(citation) });
  if (!citation) return null;
  const label = citation.citation || citation.title || "Source";
  return (
    <>
      <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
        <section
          ref={dialogRef}
          className="editor-modal citation-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="citation-preview-title"
          onMouseDown={(event) => event.stopPropagation()}
        >
          <div className="modal-heading">
            <div>
              <span className="block-kicker">{citation.sourceLabel || "Source"}</span>
              <h4 id="citation-preview-title">{label}</h4>
            </div>
            <button className="btn btn-light icon-button" type="button" aria-label="Close source preview" onClick={onClose}>
              <X size={18} />
            </button>
          </div>
          {citation.snippet && <p>{citation.snippet}</p>}
          <SourceFullViewButton citation={citation} onOpen={setCaseSource} />
        </section>
      </div>
      <SourceBrowserModal citation={caseSource} onClose={() => setCaseSource(null)} />
    </>
  );
}

/** Shared, safe Markdown renderer for AI-authored text and source citations. */
export function MarkdownResponse({ content, citations = [], className = "" }) {
  const [previewCitation, setPreviewCitation] = useState(null);
  const byNumber = citationMap(citations);

  return (
    <>
      <div className={`markdown-response ${className}`.trim()}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          urlTransform={(url) => url.startsWith("#citation-") ? url : defaultUrlTransform(url)}
          components={{
            a: ({ href, children, node: _node, ...props }) => {
              const match = href?.match(/^#citation-(\d+)$/);
              if (match && byNumber.has(match[1])) {
                return (
                  <button
                    className="inline-citation"
                    type="button"
                    onClick={() => setPreviewCitation(byNumber.get(match[1]))}
                    aria-label={`Preview source ${match[1]}`}
                  >
                    {children}
                  </button>
                );
              }
              return <a href={href} target="_blank" rel="noreferrer" {...props}>{children}</a>;
            },
          }}
        >
          {addCitationLinks(content, citations)}
        </ReactMarkdown>
      </div>
      <CitationPreviewModal citation={previewCitation} onClose={() => setPreviewCitation(null)} />
    </>
  );
}

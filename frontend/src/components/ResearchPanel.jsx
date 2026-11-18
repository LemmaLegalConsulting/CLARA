import React, { useMemo, useState } from "react";
import {
  Bot,
  BookOpen,
  Briefcase,
  Building2,
  Database,
  FileArchive,
  FileSearch,
  FileText,
  FolderOpen,
  Gavel,
  Landmark,
  Library,
  Loader2,
  Search,
  Send,
  History,
  Plus,
  Trash2,
  Upload,
} from "lucide-react";

import { api } from "../api/client.js";
import { caseCount, jurisdictionFacets, narrowResults, selectionAfterNarrowing } from "./caseJurisdictionFacets.js";
import LegalServerSaveToggle from "./LegalServerSaveToggle.jsx";
import { saveDefault } from "./legalServerSave.js";
import { LibraryBrowser } from "./LibraryBrowser.jsx";
import { ResearchSearch } from "./ResearchSearch.jsx";
import { CaseFacetBrowser, CitationPreviewModal, MarkdownResponse, SourceBrowserModal, SourceFullViewButton, isCaseLawCitation, isContentLibraryCitation } from "./MarkdownResponse.jsx";
import { isConflict } from "../api/errors.js";
import { paths } from "../routes/paths.js";
import { chatThreadView } from "../state/chatThreads.js";
import { PanelHeading } from "./PanelHeading.jsx";

const SOURCE_GROUPS = [
  {
    title: "Case",
    options: [
      { id: "case-file", label: "Case", kind: "legalserver", icon: Briefcase },
    ],
  },
  {
    title: "Example pleadings",
    options: [
      { id: "sharepoint", label: "SharePoint", kind: "sharepoint", icon: FolderOpen },
    ],
  },
  {
    title: "Case law",
    options: [
      { id: "ohio-cases", label: "Ohio Cases", kind: "local_cases", icon: Landmark },
      { id: "freelaw-project", label: "FreeLaw Project", kind: "local_cases", icon: Gavel },
    ],
  },
  {
    title: "Statutes",
    options: [
      { id: "ohio-statutes", label: "Ohio Statutes and Administrative Code", kind: "rag", icon: FileText },
      { id: "federal-housing-law", label: "Federal housing law (U.S.C. and 24 C.F.R.)", kind: "rag", icon: FileText },
    ],
  },
  {
    title: "Local law",
    options: [
      { id: "ohio-ordinances", label: "Ordinances and local rules", kind: "rag", icon: Building2 },
    ],
  },
  {
    title: "User resources",
    options: [
      { id: "user-resources", label: "User Resources", kind: "user_resources", icon: FileArchive },
    ],
  },
  {
    title: "Handbooks and treatises",
    options: [
      { id: "treatise", label: "Treatise", kind: "rag", icon: BookOpen },
      { id: "hud-handbook", label: "HUD Handbook", kind: "rag", icon: Library },
      { id: "green-book", label: "Green Book", kind: "rag", icon: BookOpen },
    ],
  },
];

function availableSourceGroups(sources) {
  const availableKinds = new Set(sources.map((source) => source.kind));
  return SOURCE_GROUPS
    .map((group) => ({
      ...group,
      options: group.options.filter((option) => availableKinds.has(option.kind)),
    }))
    .filter((group) => group.options.length);
}

// Which research view a route shows, and which tab sits under a source that
// was opened on top of it.
const TAB_FOR_VIEW = { search: "search", chats: "ask", chat: "ask", library: "browse", passage: "browse" };

// `route` is what the URL names -- the tab, a research thread, or a decision
// or library passage open in the source viewer -- and `onNavigate(path,
// options)` moves it. Search text never goes in the URL.
export function ResearchPanel({ matter, sources, onResults, legalserverSave = null, route = {}, locationState = null, onNavigate }) {
  const [query, setQuery] = useState("");
  const [lastQuery, setLastQuery] = useState("");
  const [selectedSourceIds, setSelectedSourceIds] = useState([]);
  const [results, setResults] = useState([]);
  const [sourceDecision, setSourceDecision] = useState(null);
  const [searchAugmentation, setSearchAugmentation] = useState(null);
  const [selectedResultIds, setSelectedResultIds] = useState([]);
  const [caseJurisdiction, setCaseJurisdiction] = useState("");
  const [messages, setMessages] = useState([]);
  const [useAi, setUseAi] = useState(true);
  const [sourceMode, setSourceMode] = useState("auto");
  const [showHistory, setShowHistory] = useState(true);
  const [threads, setThreads] = useState([]);
  const [currentThreadId, setCurrentThreadId] = useState(null);
  const [threadMissing, setThreadMissing] = useState(false);
  const [staleThread, setStaleThread] = useState(false);
  const threadView = chatThreadView({ routeThreadId: route.threadId ?? null, currentThreadId });
  const selectedThreadId = threadView.readOnly ? String(route.threadId) : "";
  const [resources, setResources] = useState([]);
  const [resourceTitle, setResourceTitle] = useState("");
  const [resourceType, setResourceType] = useState("case");
  const [resourceFile, setResourceFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [saveToLegalServer, setSaveToLegalServer] = useState(saveDefault(legalserverSave, "research"));
  const [delivery, setDelivery] = useState(null);
  const jurisdictionChips = useMemo(() => jurisdictionFacets(results), [results]);
  const totalCaseCount = useMemo(() => caseCount(results), [results]);
  const visibleResults = useMemo(() => narrowResults(results, caseJurisdiction), [results, caseJurisdiction]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [showUploadForm, setShowUploadForm] = useState(false);
  const [error, setError] = useState("");
  const [previewCitation, setPreviewCitation] = useState(null);
  const [caseSourceCitation, setCaseSourceCitation] = useState(null);
  // Three jobs, not one crowded form. Searching returns the corpus's own text
  // and never calls a model; asking puts a model between the reader and the
  // corpus; browsing starts from the shelf rather than from a question. They
  // share the source viewer, not the panel -- and searching is the default,
  // because a research library should answer without being asked to generate.
  const view = TAB_FOR_VIEW[route.view] || locationState?.tab || "search";
  const tabPath = { search: paths.research(), ask: paths.researchChats(), browse: paths.researchLibrary() };
  const setView = (tab) => onNavigate?.(tabPath[tab]);

  // A decision or a library passage opened in the source viewer has its own
  // URL; the tab it was opened from stays underneath. Other sources open in
  // place, as before.
  const routeCitation = route.view === "decision" && route.decisionId
    ? (locationState?.citation?.metadata?.decisionId === route.decisionId ? locationState.citation : { sourceKind: "local_cases", title: "", metadata: { decisionId: route.decisionId } })
    : route.view === "passage" && route.documentSlug && route.chunkId
      ? (locationState?.citation?.metadata?.chunkId === route.chunkId ? locationState.citation : { sourceKind: "rag", title: "", metadata: { documentSlug: route.documentSlug, chunkId: route.chunkId } })
      : null;

  function openSource(citation) {
    const state = { tab: view, citation, fromApp: true };
    if (isCaseLawCitation(citation)) {
      onNavigate?.(paths.researchDecision(Number(citation.metadata.decisionId)), { state });
      return;
    }
    if (isContentLibraryCitation(citation)) {
      onNavigate?.(paths.researchPassage(citation.metadata.documentSlug, citation.metadata.chunkId), { state });
      return;
    }
    setCaseSourceCitation(citation);
  }

  function closeRouteSource() {
    if (locationState?.fromApp) onNavigate?.(-1);
    else onNavigate?.(tabPath[view], { replace: true });
  }

  React.useEffect(() => {
    let cancelled = false;
    api.userResources()
      .then((response) => {
        if (!cancelled) setResources(response.resources || []);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Reading only; a thread named in the URL is that thread or a notice.
  const routeThreadId = route.threadId ?? null;
  React.useEffect(() => {
    const controller = new AbortController();
    setThreadMissing(false);
    setStaleThread(false);
    setHistoryLoading(true);
    api.researchHistory(routeThreadId, { signal: controller.signal })
      .then((response) => {
        setMessages(response.messages || []);
        setThreads(response.threads || []);
        setCurrentThreadId(response.currentThreadId ?? null);
      })
      .catch((err) => {
        if (controller.signal.aborted || err?.name === "AbortError") return;
        if (err.status === 404) {
          setMessages([]);
          setThreadMissing(true);
          return;
        }
        setError(err.message || "Could not load research history.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setHistoryLoading(false);
      });
    return () => controller.abort();
  }, [routeThreadId]);

  const sourceGroups = availableSourceGroups(sources);
  const sourceOptions = sourceGroups.flatMap((group) => group.options);
  const selectedKinds = [...new Set(
    selectedSourceIds
      .map((sourceId) => sourceOptions.find((option) => option.id === sourceId)?.kind)
      .filter(Boolean)
  )];
  const hasCaseLawSource = sourceOptions.some((option) => option.id === "ohio-cases");

  function toggleSource(sourceId) {
    setSelectedSourceIds((current) => current.includes(sourceId) ? current.filter((item) => item !== sourceId) : [...current, sourceId]);
  }

  async function runSearch(event) {
    event.preventDefault();
    const trimmedQuery = query.trim();
    if (!trimmedQuery) return;
    setBusy(true);
    setError("");
    const nextMessages = [...messages, { role: "user", content: trimmedQuery }];
    if (useAi) {
      setMessages(nextMessages);
    }
    try {
      const response = await api.research({
        query: trimmedQuery,
        matterId: matter?.id,
        jurisdiction: matter?.jurisdiction,
        sourceKinds: sourceMode === "cases" ? ["local_cases"] : sourceMode === "manual" && selectedKinds.length ? selectedKinds : undefined,
        sourceIds: sourceMode === "cases" ? ["ohio-cases"] : sourceMode === "manual" && selectedSourceIds.length ? selectedSourceIds : undefined,
        sourceMode,
        useAi,
        saveToLegalServer: Boolean(matter) && saveToLegalServer,
        // A question continues the thread on screen, and is refused rather
        // than filed under another if that thread is no longer current.
        ...(useAi && threadView.writeThreadId ? { threadId: threadView.writeThreadId } : {}),
      });
      if (response.threadId !== undefined) setCurrentThreadId(response.threadId);
      setDelivery(response.legalserver || null);
      setLastQuery(trimmedQuery);
      setResults(response.results);
      setSearchAugmentation(response.searchAugmentation || null);
      if (sourceMode === "auto") {
        setSelectedSourceIds(response.selectedSourceIds || []);
        setSourceDecision(response.sourceDecision || null);
      } else {
        setSourceDecision(null);
      }
      setCaseJurisdiction("");
      setSelectedResultIds(response.results.map((result) => result.id));
      onResults(response.results);
      if (useAi && response.answer) {
        setMessages([...nextMessages, { role: "assistant", content: response.answer, citations: response.results }]);
      }
      setQuery("");
    } catch (err) {
      if (isConflict(err)) {
        setStaleThread(true);
      } else {
        setError(err.message || "Source search failed.");
      }
      if (useAi) {
        setMessages(messages);
      }
    } finally {
      setBusy(false);
    }
  }

  // onResults reaches into the parent, so it must not run inside a state
  // updater: React calls those during render, and updating another component
  // from there warns and can drop the update.
  function applySelection(nextIds) {
    setSelectedResultIds(nextIds);
    onResults(results.filter((item) => nextIds.includes(item.id)));
  }

  function toggleResult(result) {
    applySelection(
      selectedResultIds.includes(result.id)
        ? selectedResultIds.filter((id) => id !== result.id)
        : [...selectedResultIds, result.id],
    );
  }

  // Narrowing hides cases, so it has to unselect them too: a source the reader
  // has visibly set aside must not still reach the draft.
  function narrowToJurisdiction(value) {
    const next = value === caseJurisdiction ? "" : value;
    setCaseJurisdiction(next);
    applySelection(selectionAfterNarrowing(results, next, selectedResultIds));
  }

  async function clearHistory() {
    // Clear deletes, unlike New chat, which keeps the old thread; ask first.
    if (!window.confirm("Delete this conversation? It cannot be recovered. New chat keeps it instead.")) return;
    setBusy(true);
    try { await api.clearResearchHistory(threadView.writeThreadId); setMessages([]); setShowHistory(false); }
    catch (err) {
      if (isConflict(err)) setStaleThread(true);
      else setError(err.message || "Could not clear research history.");
    }
    finally { setBusy(false); }
  }

  async function newChat() {
    setBusy(true);
    try {
      const response = await api.newResearchChat();
      setMessages([]);
      setThreads(response.threads || []);
      setCurrentThreadId(null);
      setShowHistory(true);
      if (routeThreadId != null) onNavigate?.(paths.researchChats());
    }
    catch (err) { setError(err.message || "Could not start a new research chat."); }
    finally { setBusy(false); }
  }

  function selectThread(event) {
    const next = event.target.value;
    onNavigate?.(next ? paths.researchChat(Number(next)) : paths.researchChats());
  }

  async function uploadResource(event) {
    event.preventDefault();
    // Held before the await: React clears currentTarget once the handler yields.
    const form = event.currentTarget;
    if (!resourceFile) return;
    setUploadBusy(true);
    setError("");
    try {
      const formData = new FormData();
      formData.append("title", resourceTitle || resourceFile.name);
      formData.append("resourceType", resourceType);
      formData.append("file", resourceFile);
      const response = await api.createUserResource(formData);
      setResources((current) => [response.resource, ...current.filter((item) => item.id !== response.resource.id)]);
      setResourceTitle("");
      setResourceFile(null);
      setShowUploadForm(false);
      form.reset();
    } catch (err) {
      setError(err.message || "Reference upload failed.");
    } finally {
      setUploadBusy(false);
    }
  }

  return (
    <div className="panel">
      <PanelHeading
        title="Research"
        description="Search the corpus without AI, ask a question and get a cited answer, or browse what has been imported."
      />
      <div className="research-view-tabs" role="tablist" aria-label="Research view">
        <button type="button" role="tab" aria-selected={view === "search"} className={view === "search" ? "selected" : ""} onClick={() => setView("search")}>
          <Search size={15} /> Search the corpus
        </button>
        <button type="button" role="tab" aria-selected={view === "ask"} className={view === "ask" ? "selected" : ""} onClick={() => setView("ask")}>
          <Bot size={15} /> Ask a question
        </button>
        <button type="button" role="tab" aria-selected={view === "browse"} className={view === "browse" ? "selected" : ""} onClick={() => setView("browse")}>
          <Library size={15} /> Browse the library
        </button>
      </div>
      {view === "search" && <ResearchSearch matter={matter} onOpenSource={openSource} />}
      {view === "browse" && <LibraryBrowser onOpenSource={openSource} />}
      {view === "ask" && (
      <>
      <div className="private-reference-panel">
        {resources.length > 0 && (
          <div className="reference-list">
            {resources.slice(0, 4).map((resource) => (
              <small key={resource.id}>{resource.title} · {resource.resourceType}</small>
            ))}
          </div>
        )}
        <details
          className="disclosure"
          open={showUploadForm}
          onToggle={(event) => setShowUploadForm(event.currentTarget.open)}
        >
          <summary>
            Upload a reference
            <span className="disclosure-summary">
              {resources.length ? `${resources.length} private reference${resources.length === 1 ? "" : "s"}` : "None yet"}
            </span>
          </summary>
          <form className="reference-upload-form disclosure-body" onSubmit={uploadResource}>
            <div className="reference-upload-grid">
              <label className="field">
                <span>Private reference</span>
                <input className="form-control" value={resourceTitle} onChange={(event) => setResourceTitle(event.target.value)} />
                <small className="field-help">Optional. If blank, the file name will be used.</small>
              </label>
              <label className="field">
                <span>Type</span>
                <select className="form-select" value={resourceType} onChange={(event) => setResourceType(event.target.value)}>
                  <option value="case">Case</option>
                  <option value="brief">Example brief</option>
                  <option value="example">Example filing</option>
                  <option value="other">Other</option>
                </select>
              </label>
            </div>
            <label className="field">
              <span>Reference file</span>
              <input
                type="file"
                accept=".txt,.md,.csv,.json,.html,.htm,.docx,.pdf,text/*,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                onChange={(event) => setResourceFile(event.target.files?.[0] || null)}
              />
            </label>
            <button className="btn btn-light full" type="submit" disabled={uploadBusy || !resourceFile}>
              {uploadBusy ? <Loader2 className="spin" size={16} /> : <Upload size={16} />} Upload private reference
            </button>
          </form>
        </details>
      </div>
      {threadMissing && (
        <div className="empty-state compact-empty" role="status">
          <strong className="empty-state-title">This conversation is not available</strong>
          <p>No research thread with this number is yours. No other conversation is shown in its place.</p>
          <button className="btn btn-primary" type="button" onClick={() => onNavigate?.(paths.researchChats())}>Open the current chat</button>
        </div>
      )}
      {staleThread && (
        <div className="inline-error" role="alert">
          A new chat was started in another window, so this conversation is no longer the current one. Your question was not sent.
          <button className="btn btn-light" type="button" onClick={() => { setStaleThread(false); onNavigate?.(paths.researchChats()); }}>Open the current chat</button>
        </div>
      )}
      <form className="research-chat-form" onSubmit={runSearch} hidden={threadMissing}>
        <div className="chat-history-actions">
          <select className="form-select" aria-label="Research chat threads" value={selectedThreadId} onChange={selectThread}>
            <option value="">Current chat</option>
            {threads.filter((thread) => !thread.active).map((thread) => <option key={thread.id} value={thread.id}>{thread.preview}</option>)}
          </select>
          <button className="text-link-button" type="button" onClick={() => setShowHistory((value) => !value)}><History size={15} /> {showHistory ? "Hide history" : "View history"}</button>
          <button className="text-link-button" type="button" disabled={busy} onClick={newChat}><Plus size={15} /> New chat</button>
          <button className="text-link-button danger" type="button" disabled={busy || !!selectedThreadId || !messages.length} onClick={clearHistory}><Trash2 size={15} /> Clear</button>
        </div>
        {useAi && showHistory && messages.length > 0 && (
          <div className="research-transcript" aria-label="Research conversation">
            {messages.map((message, index) => (
              <div key={`${message.role}-${index}`} className={`research-message ${message.role}`}>
                <strong>{message.role === "assistant" ? "AI" : "You"}</strong>
                {message.role === "assistant" ? (
                  <MarkdownResponse content={message.content} citations={message.citations} />
                ) : <p>{message.content}</p>}
              </div>
            ))}
          </div>
        )}
        <details className="disclosure">
          <summary>
            Search options
            <span className="disclosure-summary">{useAi ? "AI answer" : "Results only"} · {sourceMode === "auto" ? "auto sources" : sourceMode === "cases" ? "cases only" : "chosen sources"}</span>
          </summary>
          <div className="disclosure-body">
            <div className="research-mode-row">
              <label className="research-ai-toggle">
                <input
                  type="checkbox"
                  checked={useAi}
                  onChange={(event) => setUseAi(event.target.checked)}
                />
                <span>{useAi ? <Bot size={16} /> : <Database size={16} />} AI answer</span>
              </label>
              <small>{useAi ? "Ask connected sources and get a cited answer." : "Retrieve matching results only."}</small>
            </div>
            <div className="research-mode-row">
              <label className="research-ai-toggle"><input type="radio" checked={sourceMode === "auto"} onChange={() => setSourceMode("auto")} /><span>Auto sources</span></label>
              {hasCaseLawSource && (
                <label className="research-ai-toggle"><input type="radio" checked={sourceMode === "cases"} onChange={() => setSourceMode("cases")} /><span>Cases only</span></label>
              )}
              <label className="research-ai-toggle"><input type="radio" checked={sourceMode === "manual"} onChange={() => setSourceMode("manual")} /><span>Choose sources</span></label>
              <small>{sourceMode === "auto" ? "Automatically routes this question to relevant sources." : sourceMode === "cases" ? "Searches only the imported case-law corpus." : "Only the selected sources will be searched."}</small>
            </div>
          </div>
        </details>
        {sourceMode === "auto" && sourceDecision && (
          <aside className="source-decision" aria-label="Automatic source decision">
            <strong>Auto-source decision</strong>
            <p>{sourceDecision.summary}</p>
            <ul>
              {sourceDecision.sources.map((source) => (
                <li key={source.id}>
                  <span><b>{source.label}</b> — {source.reason}</span>
                  <small>{source.resultCount} result{source.resultCount === 1 ? "" : "s"}</small>
                </li>
              ))}
            </ul>
          </aside>
        )}
        {searchAugmentation?.expanded && (
          <aside className="source-decision" aria-label="Search expansion decision">
            <strong>Search expanded</strong>
            <p>{searchAugmentation.finalEvaluation?.adequate ? "Additional related sources were added." : "Additional related sources were tried, but coverage may still be incomplete."}</p>
            <ul>
              {searchAugmentation.rounds.slice(1).map((round, index) => (
                <li key={`${round.query}-${index}`}>
                  <span><b>{round.resultCount} result{round.resultCount === 1 ? "" : "s"}</b> — {round.reason}</span>
                  <small>{round.query}</small>
                </li>
              ))}
            </ul>
          </aside>
        )}
        <div className="research-question">
          <label className="field">
            <span>Research question</span>
            <textarea className="form-control"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <p className="field-example">Example: Can a tenant raise habitability conditions as a defense when rent is disputed and rental assistance is pending?</p>
          {matter && (
            <LegalServerSaveToggle
              kind="research"
              checked={saveToLegalServer}
              onChange={setSaveToLegalServer}
              bootstrapSave={legalserverSave}
              delivery={delivery}
              disabled={busy}
            />
          )}
        </div>
        {sourceMode === "manual" && sourceGroups.length > 0 && (
          <div className="source-picker" aria-label="Research sources">
            <div className="source-toggles">
              {sourceGroups.map((group) => (
                <React.Fragment key={group.title}>
                  {group.options.map((source) => {
                    const Icon = source.icon || FileSearch;
                    const selected = selectedSourceIds.includes(source.id);
                    return (
                      <button
                        key={source.id}
                        className={selected ? "selected" : ""}
                        type="button"
                        aria-pressed={selected}
                        title={source.label}
                        onClick={() => toggleSource(source.id)}
                      >
                        <Icon size={18} aria-hidden="true" />
                        <span>{source.label}</span>
                        <small>{group.title}</small>
                      </button>
                    );
                  })}
                </React.Fragment>
              ))}
            </div>
          </div>
        )}
        <button className="btn btn-primary full" type="submit" disabled={busy || historyLoading || !!selectedThreadId || !query.trim()}>
          {busy ? <Loader2 className="spin" size={16} /> : useAi ? <Send size={16} /> : <Search size={16} />}
          {useAi ? "Ask sources" : "Search sources"}
        </button>
      </form>
      {error && <div className="inline-error">{error}</div>}
      {results.length > 0 && (
        <CaseFacetBrowser
          key={lastQuery}
          compact
          initialQuery={lastQuery || results.find((result) => result.sourceKind === "local_cases")?.title || ""}
          onOpenSource={openSource}
        />
      )}
      {jurisdictionChips.length > 0 && (
        <div className="jurisdiction-narrowing">
          <span className="jurisdiction-narrowing-label">Cases from</span>
          <button
            type="button"
            className={`jurisdiction-chip ${caseJurisdiction ? "" : "active"}`}
            aria-pressed={!caseJurisdiction}
            onClick={() => narrowToJurisdiction("")}
          >
            Everywhere <span className="jurisdiction-chip-count">{totalCaseCount}</span>
          </button>
          {jurisdictionChips.map((chip) => (
            <button
              key={chip.value || "unattributed"}
              type="button"
              className={`jurisdiction-chip ${caseJurisdiction === chip.value ? "active" : ""}`}
              aria-pressed={caseJurisdiction === chip.value}
              onClick={() => narrowToJurisdiction(chip.value)}
            >
              {chip.label} <span className="jurisdiction-chip-count">{chip.count}</span>
            </button>
          ))}
          <small>These are persuasive, not binding — a case from another county may still be the best one.</small>
        </div>
      )}
      <div className="result-list">
        {visibleResults.map((result) => (
          <article key={result.id} className="result-card">
            <label className="result-select">
              <input
                type="checkbox"
                checked={selectedResultIds.includes(result.id)}
                onChange={() => toggleResult(result)}
              />
              <span>
                <strong>{result.title}</strong>
                <p>{result.snippet}</p>
                <small>{result.sourceLabel}{result.citation ? ` · ${result.citation}` : ""}</small>
              </span>
            </label>
            <div className="result-source-actions">
              <button className="text-link-button" type="button" onClick={() => setPreviewCitation(result)}>
                Preview citation
              </button>
              <SourceFullViewButton citation={result} onOpen={openSource} />
            </div>
          </article>
        ))}
      </div>
      </>
      )}
      <CitationPreviewModal citation={previewCitation} onClose={() => setPreviewCitation(null)} />
      <SourceBrowserModal citation={caseSourceCitation} onClose={() => setCaseSourceCitation(null)} />
      <SourceBrowserModal citation={routeCitation} onClose={closeRouteSource} />
    </div>
  );
}

import { ApiError } from "./errors.js";
import { adminUrlFrom } from "./urls.js";
import { trackRequest } from "./wakeNotice.js";

export const API_BASE = import.meta.env.VITE_API_BASE || "/api";

function getCookie(name) {
  const match = document.cookie
    .split("; ")
    .find((item) => item.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : "";
}

function errorMessageFrom(text, response) {
  const fallback = `Request failed: ${response.status}`;
  try {
    return JSON.parse(text).error || fallback;
  } catch {
    // Not JSON. An HTML body is a server error page rather than anything worth
    // showing an advocate, so only plain text is passed through.
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("text/html") || /^\s*</.test(text)) return fallback;
    return text || fallback;
  }
}

async function request(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const unsafe = !["GET", "HEAD", "OPTIONS", "TRACE"].includes(method);
  const csrfToken = unsafe ? getCookie("csrftoken") : "";

  // Closed once the response starts arriving, not once its body is read: a
  // sleeping server is awake the moment it answers at all, and reading a large
  // body afterwards is not a wait anyone needs the wake-up notice for.
  const settled = trackRequest();
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      credentials: "include",
      headers: {
        ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        ...(csrfToken ? { "X-CSRFToken": csrfToken } : {}),
        ...(options.headers || {}),
      },
      ...options,
    });
  } catch (err) {
    // A request the caller cancelled -- the advocate moved to another case
    // before this one answered -- is not a server problem, and must never feed
    // the "server is waking up" retry loop.
    if (err?.name === "AbortError") throw err;
    // fetch rejects only when there was no response at all -- the host is
    // unreachable, the connection dropped, DNS failed. Status 0 says exactly
    // that, and is what tells a caller this is worth asking again about.
    throw new ApiError(err?.message || "The server could not be reached", { status: 0 });
  } finally {
    settled();
  }

  if (!response.ok) {
    const text = await response.text();
    let data = null;
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
    throw new ApiError(errorMessageFrom(text, response), { status: response.status, data });
  }

  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    return response;
  }
  return response.json();
}

export const api = {
  fillCatalog: (matterId) => request(`/template-fill/?${new URLSearchParams({ matterId })}`),
  startTemplateFill: (payload) => request("/template-fill/start/", { method: "POST", body: payload instanceof FormData ? payload : JSON.stringify(payload) }),
  // `caseKey` names the case the URL is on; the server refuses a session on another.
  fillSession: (id, { caseKey = "" } = {}) => request(`/template-fill/sessions/${id}/${caseKey ? `?${new URLSearchParams({ caseKey })}` : ""}`),
  previewTemplateFill: (id, answers) => request(`/template-fill/sessions/${id}/preview/`, { method: "POST", body: JSON.stringify({ answers }) }),
  saveTemplateFill: (id, payload, exporting = false) => request(`/template-fill/sessions/${id}/`, { method: exporting ? "POST" : "PATCH", body: JSON.stringify(payload) }),
  fillJob: (id) => request(`/template-fill/jobs/${id}/`),
  downloadTemplateFill: (id) => request(`/template-fill/jobs/${id}/file/`),
  bootstrap: () => request("/bootstrap/"),
  me: () => request("/auth/me/"),
  authorProfile: () => request("/author-profile/"),
  updateAuthorProfile: (payload) => request("/author-profile/", { method: "PATCH", body: JSON.stringify(payload) }),
  login: (payload) => request("/auth/login/", { method: "POST", body: JSON.stringify(payload) }),
  logout: () => request("/auth/logout/", { method: "POST" }),
  changePassword: (payload) => request("/auth/change-password/", { method: "POST", body: JSON.stringify(payload) }),
  // `returnTo` is the page to come back to; the server keeps it beside the
  // OAuth state and accepts only an in-app route.
  startOffice365Login: (returnTo = "") => request(`/auth/office365/start/${returnTo ? `?${new URLSearchParams({ returnTo })}` : ""}`),
  cases: ({ query = "", status = "", assigned = "", problem = "", sort = "", limit = 0, offset = 0 } = {}) => {
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    if (status) params.set("status", status);
    if (assigned) params.set("assigned", assigned);
    if (problem) params.set("problem", problem);
    if (sort) params.set("sort", sort);
    if (limit) params.set("limit", String(limit));
    if (offset) params.set("offset", String(offset));
    const search = params.toString();
    return request(`/cases/${search ? `?${search}` : ""}`);
  },
  createManualCase: (formData) => request("/cases/", { method: "POST", body: formData }),
  updateManualCase: (matterId, payload) =>
    request(`/cases/${matterId}/`, { method: "PATCH", body: JSON.stringify(payload) }),
  legalserverDraftIntakePreview: (matterId) =>
    request(`/cases/${matterId}/`, { method: "POST", body: JSON.stringify({ action: "legalserver_draft_intake" }) }),
  triageRubrics: () => request("/triage/rubrics/"),
  caseTriage: (matterId) => request(`/cases/${matterId}/triage/`),
  runCaseTriage: (matterId, payload) =>
    request(`/cases/${matterId}/triage/`, { method: "POST", body: JSON.stringify(payload) }),
  connectLegalServer: (payload) => request("/legalserver/account/", { method: "POST", body: JSON.stringify(payload) }),
  disconnectLegalServer: () => request("/legalserver/account/", { method: "DELETE" }),
  caseDetail: (matterId) => request(`/cases/${matterId}/`),
  // The case a URL names by its readable number. Read-only; `signal` lets a
  // superseded lookup be cancelled.
  caseByRouteKey: (caseKey, { signal } = {}) => request(`/cases/by-route-key/?${new URLSearchParams({ key: caseKey })}`, { signal }),
  // Read-only. A thread id is that thread or a 404, never a stand-in.
  caseChatHistory: (matterId, threadId, { signal } = {}) => request(`/cases/${matterId}/chat/${threadId ? `?threadId=${threadId}` : ""}`, { signal }),
  newCaseChat: (matterId) => request(`/cases/${matterId}/chat/`, { method: "POST", body: JSON.stringify({ action: "new_thread" }) }),
  clearCaseChatHistory: (matterId, threadId = null) => request(`/cases/${matterId}/chat/${threadId ? `?threadId=${threadId}` : ""}`, { method: "DELETE" }),
  caseChat: (matterId, payload) => request(`/cases/${matterId}/chat/`, { method: "POST", body: JSON.stringify(payload) }),
  caseLegalServer: (matterId) => request(`/cases/${matterId}/legalserver/`),
  saveCaseNoteToLegalServer: (matterId, payload) =>
    request(`/cases/${matterId}/legalserver/casenote/`, { method: "POST", body: JSON.stringify(payload) }),
  saveDocumentToLegalServer: (matterId, formData) =>
    request(`/cases/${matterId}/legalserver/document/`, { method: "POST", body: formData }),
  caseDocuments: (matterId) => request(`/cases/${matterId}/documents/`),
  caseMaterials: (matterId) => request(`/cases/${matterId}/materials/`),
  customFields: (matterId) => request(`/cases/${matterId}/custom-fields/`),
  fetchCustomFields: (matterId, payload) =>
    request(`/cases/${matterId}/custom-fields/fetch/`, { method: "POST", body: JSON.stringify(payload) }),
  caseFacts: (matterId) => request(`/cases/${matterId}/facts/`),
  recommendCaseFacts: (matterId) => request(`/cases/${matterId}/facts/recommend/`, { method: "POST" }),
  createCaseFact: (matterId, payload) =>
    request(`/cases/${matterId}/facts/`, { method: "POST", body: JSON.stringify(payload) }),
  uploadCaseFactDocument: (matterId, formData) =>
    request(`/cases/${matterId}/facts/`, { method: "POST", body: formData }),
  caseDocumentContext: (matterId, documentId, payload) =>
    request(`/cases/${matterId}/documents/${documentId}/context/`, { method: "POST", body: JSON.stringify(payload) }),
  caseDocumentFileUrl: (matterId, documentId) =>
    `${API_BASE}/cases/${encodeURIComponent(matterId)}/documents/${encodeURIComponent(documentId)}/file/`,
  candidateIssues: (matterId) => request(`/cases/${matterId}/candidate-issues/`),
  runIssueSelection: (matterId, payload) =>
    request(`/cases/${matterId}/run-issue-selection/`, { method: "POST", body: JSON.stringify(payload) }),
  reviewCandidateIssue: (issueId, payload) =>
    request(`/candidate-issues/${issueId}/review/`, { method: "POST", body: JSON.stringify(payload) }),
  modes: () => request("/modes/"),
  templates: () => request("/templates/"),
  userResources: () => request("/user-resources/"),
  createUserResource: (formData) => request("/user-resources/", { method: "POST", body: formData }),
  caselawDecision: (decisionId) => request(`/caselaw/decisions/${decisionId}/`),
  caselawDecisionPdfUrl: (decisionId) => `${API_BASE}/caselaw/decisions/${decisionId}/pdf/`,
  caselawBrowse: (params = {}) => request(`/caselaw/browse/${Object.keys(params).length ? `?${new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== ""))}` : ""}`),
  // Facet narrowing repeats a parameter name per selected value, so the catalog
  // takes pairs rather than an object.
  caselawCatalog: (params = []) => request(`/caselaw/catalog/${params.length ? `?${new URLSearchParams(params)}` : ""}`),
  sharepointPrecedents: (query, { signal } = {}) => request(`/sharepoint/precedents/?${new URLSearchParams({ q: query, limit: "25" })}`, { signal }),
  library: () => request("/library/"),
  libraryDocument: (documentSlug, query = "") =>
    request(`/library/${encodeURIComponent(documentSlug)}/${query ? `?${new URLSearchParams({ q: query })}` : ""}`),
  contentSource: (documentSlug, chunkId) => request(`/sources/content/${encodeURIComponent(documentSlug)}/${encodeURIComponent(chunkId)}/`),
  contentSourceRelated: (documentSlug, chunkId) =>
    request(`/sources/content/${encodeURIComponent(documentSlug)}/${encodeURIComponent(chunkId)}/related/`),
  ordinanceCoverage: () => request("/ordinances/coverage/"),
  ordinanceDataset: (name) => request(`/ordinances/datasets/${encodeURIComponent(name)}/`),
  contentSourcePdfUrl: (documentSlug, chunkId, page) =>
    `${API_BASE}/sources/content/${encodeURIComponent(documentSlug)}/${encodeURIComponent(chunkId)}/pdf/${page ? `#page=${page}` : ""}`,
  // Read-only. A thread id is that thread or a 404, never a stand-in.
  researchHistory: (threadId, { signal } = {}) => request(`/research/${threadId ? `?threadId=${threadId}` : ""}`, { signal }),
  newResearchChat: () => request("/research/", { method: "POST", body: JSON.stringify({ action: "new_thread" }) }),
  clearResearchHistory: (threadId = null) => request(`/research/${threadId ? `?threadId=${threadId}` : ""}`, { method: "DELETE" }),
  research: (payload) => request("/research/", { method: "POST", body: JSON.stringify(payload) }),
  // Deterministic search. Separate from `research` on purpose: that endpoint is
  // the chat, this one never calls a model unless the payload asks it to, and
  // says in every response whether one ran.
  researchSearch: (payload) => request("/research/search/", { method: "POST", body: JSON.stringify(payload) }),
  researchSearchStatus: () => request("/research/search/status/"),
  createTemplateFromExample: (payload) =>
    request("/templates/from-example/", { method: "POST", body: JSON.stringify(payload) }),
  createSession: (payload) => request("/drafting-sessions/", { method: "POST", body: JSON.stringify(payload) }),
  // Saved sessions for one case, as light rows. Read-only.
  savedSessions: ({ caseKey, workspace = "drafting", limit = 20, offset = 0 }, { signal } = {}) =>
    request(`/drafting-sessions/?${new URLSearchParams({ caseKey, workspace, limit: String(limit), offset: String(offset) })}`, { signal }),
  // One saved session and where reopening it should land. `caseKey` and
  // `workspace` name the URL it was opened from; the server checks both.
  // Save the advocate's choices on a session. `revision` is the one they were
  // made against; a 409 means another window saved first.
  checkpointSession: (sessionId, payload) =>
    request(`/drafting-sessions/${sessionId}/`, { method: "PATCH", body: JSON.stringify(payload) }),
  savedSession: (sessionId, { caseKey = "", workspace = "" } = {}, { signal } = {}) => {
    const params = new URLSearchParams();
    if (caseKey) params.set("caseKey", caseKey);
    if (workspace) params.set("workspace", workspace);
    const query = params.toString();
    return request(`/drafting-sessions/${sessionId}/${query ? `?${query}` : ""}`, { signal });
  },
  advanceSession: (sessionId, payload) =>
    request(`/drafting-sessions/${sessionId}/advance/`, { method: "POST", body: JSON.stringify(payload) }),
  recommendSessionFacts: (sessionId, payload = { apply: true }) =>
    request(`/drafting-sessions/${sessionId}/recommend-facts/`, { method: "POST", body: JSON.stringify(payload) }),
  recommendSessionGoals: (sessionId, payload = { limit: 5 }) =>
    request(`/drafting-sessions/${sessionId}/recommend-goals/`, { method: "POST", body: JSON.stringify(payload) }),
  recommendSessionSupport: (sessionId, payload = { apply: true }) =>
    request(`/drafting-sessions/${sessionId}/recommend-support/`, { method: "POST", body: JSON.stringify(payload) }),
  sessionOutline: (sessionId) => request(`/drafting-sessions/${sessionId}/outline/`),
  opposingFiling: (sessionId, { signal } = {}) => request(`/drafting-sessions/${sessionId}/opposing-filing/`, { signal }),
  chooseOpposingFiling: (sessionId, payload) =>
    request(`/drafting-sessions/${sessionId}/opposing-filing/`, { method: "PUT", body: JSON.stringify(payload) }),
  uploadOpposingFiling: (sessionId, formData) =>
    request(`/drafting-sessions/${sessionId}/opposing-filing/`, { method: "POST", body: formData }),
  updateOpposingFiling: (sessionId, payload) =>
    request(`/drafting-sessions/${sessionId}/opposing-filing/`, { method: "PATCH", body: JSON.stringify(payload) }),
  removeOpposingFiling: (sessionId) => request(`/drafting-sessions/${sessionId}/opposing-filing/`, { method: "DELETE" }),
  approveSessionOutline: (sessionId, payload = {}) =>
    request(`/drafting-sessions/${sessionId}/outline/`, { method: "POST", body: JSON.stringify(payload) }),
  generateDraftPlan: (sessionId, payload = {}) =>
    request(`/drafting-sessions/${sessionId}/plan/`, { method: "POST", body: JSON.stringify(payload) }),
  updateDraftPlan: (sessionId, payload = {}) =>
    request(`/drafting-sessions/${sessionId}/plan/`, { method: "PATCH", body: JSON.stringify(payload) }),
  updateSessionTemplateData: (sessionId, templateData) =>
    request(`/drafting-sessions/${sessionId}/template-data/`, { method: "POST", body: JSON.stringify({ templateData }) }),
  sessionDrafts: (sessionId, { signal } = {}) => request(`/drafting-sessions/${sessionId}/drafts/`, { signal }),
  sessionPackage: (sessionId) => request(`/drafting-sessions/${sessionId}/package/`),
  // Answers 202 with a job; poll draftGenerationJob until it completes
  // (state/draftJobs.js does).
  generatePlanDrafts: (sessionId, payload = {}) =>
    request(`/drafting-sessions/${sessionId}/drafts/`, { method: "POST", body: JSON.stringify(payload) }),
  draftGenerationJob: (sessionId, jobId, { signal } = {}) =>
    request(`/drafting-sessions/${sessionId}/drafts/?${new URLSearchParams({ job: String(jobId) })}`, { signal }),
  generateDraft: (sessionId) => request(`/drafting-sessions/${sessionId}/draft/`, { method: "POST" }),
  updateDraft: (draftId, payload) => request(`/drafts/${draftId}/`, { method: "PATCH", body: JSON.stringify(payload) }),
  draftComponents: (draftId) => request(`/drafts/${draftId}/components/`),
  draftOperations: (draftId) => request(`/drafts/${draftId}/operations/`),
  proposeDraftOperation: (draftId, payload) =>
    request(`/drafts/${draftId}/operations/`, { method: "POST", body: JSON.stringify(payload) }),
  decideDraftOperation: (draftId, operationId, payload) =>
    request(`/drafts/${draftId}/operations/${operationId}/decision/`, { method: "POST", body: JSON.stringify(payload) }),
  regenerateDraftBlock: (draftId, blockKey, payload) =>
    request(`/drafts/${draftId}/blocks/${blockKey}/regenerate/`, { method: "POST", body: JSON.stringify(payload) }),
  validateDraft: (draftId) => request(`/drafts/${draftId}/validate/`, { method: "POST" }),
  draftRevisionPlan: (draftId) => request(`/drafts/${draftId}/revision-plan/`, { method: "POST" }),
  applyDraftRevision: (draftId, plan) =>
    request(`/drafts/${draftId}/revision/`, { method: "POST", body: JSON.stringify({ plan }) }),
  // The export is a download, so the LegalServer opt-out rides along as a query
  // parameter and the outcome comes back in response headers.
  exportDraftUrl: (draftId, { saveToLegalServer = true } = {}) =>
    `${API_BASE}/drafts/${draftId}/export/${saveToLegalServer ? "" : "?saveToLegalServer=0"}`,
  exportDraft: (draftId, options = {}) => request(api.exportDraftUrl(draftId, options).replace(API_BASE, "")),

  adviceLetterSections: ({ region = "", letterType = "brief_advice", reviewedOnly = false } = {}) =>
    request(`/advice-letters/sections/?${new URLSearchParams({ region, letterType, reviewedOnly: reviewedOnly ? "1" : "" })}`),
  adviceLetterAddressing: (matterId) =>
    request(`/advice-letters/addressing/?${new URLSearchParams({ matterId })}`),
  // Argument Gym. A workspace holds the brief under test and the case context
  // it is tested against; a run is one adversarial pass over it.
  gymWorkspaces: ({ matterId = "", query = "" } = {}) => {
    const params = new URLSearchParams();
    if (matterId) params.set("matterId", matterId);
    if (query) params.set("q", query);
    const search = params.toString();
    return request(`/argument-gym/workspaces/${search ? `?${search}` : ""}`);
  },
  gymChecks: () => request("/argument-gym/checks/"),
  saveGymCheckDefaults: (enabledChecks) =>
    request("/argument-gym/checks/defaults/", { method: "PUT", body: JSON.stringify({ enabledChecks }) }),
  resetGymCheckDefaults: () => request("/argument-gym/checks/defaults/", { method: "DELETE" }),
  gymLegalRules: () => request("/argument-gym/legal-rules/"),
  gymChecklists: () => request("/argument-gym/checklists/"),
  createGymChecklist: (payload) =>
    request("/argument-gym/checklists/", { method: "POST", body: JSON.stringify(payload) }),
  updateGymChecklist: (checklistId, payload) =>
    request(`/argument-gym/checklists/${checklistId}/`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteGymChecklist: (checklistId) =>
    request(`/argument-gym/checklists/${checklistId}/`, { method: "DELETE" }),
  gymCourts: (query = "") =>
    request(`/argument-gym/courts/${query ? `?${new URLSearchParams({ q: query })}` : ""}`),
  gymCourtDetection: (workspaceId) => request(`/argument-gym/workspaces/${workspaceId}/court-detection/`),
  createGymWorkspace: (payload) => request("/argument-gym/workspaces/", { method: "POST", body: JSON.stringify(payload) }),
  gymWorkspace: (workspaceId) => request(`/argument-gym/workspaces/${workspaceId}/`),
  updateGymWorkspace: (workspaceId, payload) =>
    request(`/argument-gym/workspaces/${workspaceId}/`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteGymWorkspace: (workspaceId) => request(`/argument-gym/workspaces/${workspaceId}/`, { method: "DELETE" }),
  uploadGymDocument: (workspaceId, formData) =>
    request(`/argument-gym/workspaces/${workspaceId}/documents/`, { method: "POST", body: formData }),
  attachGymDraft: (workspaceId, payload) =>
    request(`/argument-gym/workspaces/${workspaceId}/documents/`, { method: "POST", body: JSON.stringify(payload) }),
  updateGymDocument: (documentId, payload) =>
    request(`/argument-gym/documents/${documentId}/`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteGymDocument: (documentId) => request(`/argument-gym/documents/${documentId}/`, { method: "DELETE" }),
  gymMaterials: (workspaceId) => request(`/argument-gym/workspaces/${workspaceId}/materials/`),
  setGymMaterialExcluded: (workspaceId, payload) =>
    request(`/argument-gym/workspaces/${workspaceId}/materials/`, { method: "POST", body: JSON.stringify(payload) }),
  gymRuns: (workspaceId) => request(`/argument-gym/workspaces/${workspaceId}/runs/`),
  runArgumentGym: (workspaceId, payload = {}) =>
    request(`/argument-gym/workspaces/${workspaceId}/runs/`, { method: "POST", body: JSON.stringify(payload) }),
  gymRun: (runId) => request(`/argument-gym/runs/${runId}/`),
  gymArtifact: (runId, kind) => request(`/argument-gym/runs/${runId}/artifacts/${kind}/`),
  gymRevisionPlan: (runId, challengeIds = []) =>
    request(`/argument-gym/runs/${runId}/revision/${challengeIds.length ? `?${new URLSearchParams(challengeIds.map((id) => ["challengeId", String(id)]))}` : ""}`),
  applyGymRevision: (runId, payload) =>
    request(`/argument-gym/runs/${runId}/revision/`, { method: "POST", body: JSON.stringify(payload) }),
  setChallengeDisposition: (challengeId, payload) =>
    request(`/argument-gym/challenges/${challengeId}/`, { method: "POST", body: JSON.stringify(payload) }),
  researchChallenge: (challengeId, payload = {}) =>
    request(`/argument-gym/challenges/${challengeId}/research/`, { method: "POST", body: JSON.stringify(payload) }),
  stressTestDraft: (draftId, payload = {}) =>
    request(`/drafts/${draftId}/stress-test/`, { method: "POST", body: JSON.stringify(payload) }),

  adviceLetterRecommendations: (payload) =>
    request("/advice-letters/recommend/", { method: "POST", body: JSON.stringify(payload) }),
  adviceLetterPreview: (payload) =>
    request("/advice-letters/preview/", { method: "POST", body: JSON.stringify(payload) }),
  adviceLetterExport: (payload) =>
    request("/advice-letters/export/", { method: "POST", body: JSON.stringify(payload) }),
  adviceLetterDraft: (payload) =>
    request("/advice-letters/drafts/", { method: "POST", body: JSON.stringify(payload) }),
  // A saved letter as it was saved. Read-only: nothing is reassembled.
  adviceLetterSavedDraft: (draftId, { caseKey = "" } = {}, { signal } = {}) =>
    request(`/advice-letters/drafts/${draftId}/${caseKey ? `?${new URLSearchParams({ caseKey })}` : ""}`, { signal }),
  adviceLetterDraftExport: (draftId, payload = {}) =>
    request(`/advice-letters/drafts/${draftId}/export/`, { method: "POST", body: JSON.stringify(payload) }),
  adviceLetterDraftToLegalServer: (draftId, payload = {}) =>
    request(`/advice-letters/drafts/${draftId}/legalserver/`, { method: "POST", body: JSON.stringify(payload) }),
  adminUrl: () => adminUrlFrom(API_BASE, window.location.origin),
};

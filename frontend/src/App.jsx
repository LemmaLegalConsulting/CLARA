import TemplateFillPanel from "./components/TemplateFillPanel.jsx";
import React, { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useBlocker, useLocation, useNavigate } from "react-router";
import {
  Archive,
  ChevronDown,
  Cloud,
  CheckCircle2,
  ClipboardCheck,
  ClipboardList,
  Download,
  FileText,
  FolderOpen,
  Layers3,
  LogIn,
  LogOut,
  Link2,
  Loader2,
  Mail,
  MessageSquare,
  PenLine,
  Search,
  Settings,
  Swords,
  Unplug,
  UserRound,
  X,
} from "lucide-react";

import { api } from "./api/client.js";
import { isConflict, retryWhileUnreachable } from "./api/errors.js";
import { AuthorFields, emptyAuthorProfile } from "./components/AuthorFields.jsx";
import { AdviceLetterPanel } from "./components/AdviceLetterPanel.jsx";
import { ArgumentGymPanel } from "./components/ArgumentGymPanel.jsx";
import { AuthorProfile } from "./components/AuthorProfile.jsx";
import { ChangePassword } from "./components/ChangePassword.jsx";
import { CaseChat } from "./components/CaseChat.jsx";
import { CasePreviewModal } from "./components/CasePreviewModal.jsx";
import { DraftEditor } from "./editor/DraftEditor.jsx";
import { CaseSelector } from "./components/CaseSelector.jsx";
import { DraftSupportReview } from "./components/DraftSupportReview.jsx";
import { DraftGoalPanel } from "./components/DraftGoalPanel.jsx";
import { DraftPlanReview } from "./components/DraftPlanReview.jsx";
import { DocumentHistoryPanel } from "./components/DocumentHistoryPanel.jsx";
import { DraftSwitcher } from "./components/DraftSwitcher.jsx";
import { DraftQuestionsReview } from "./components/DraftQuestionsReview.jsx";
import { planQuestionsForReview } from "./components/planQuestions.js";
import { FactReview } from "./components/FactReview.jsx";
import { factRecommendationState } from "./components/factReviewState.js";
import { mergeFactIds } from "./components/factReviewState.js";
import { LawReview } from "./components/LawReview.jsx";
import LegalServerSaveToggle from "./components/LegalServerSaveToggle.jsx";
import { deliveryFromHeaders, saveDefault } from "./components/legalServerSave.js";
import { saveResponseAsFile } from "./components/downloadFile.js";
import { PackagePanel } from "./components/PackagePanel.jsx";
import { ResearchPanel } from "./components/ResearchPanel.jsx";
import { RevisionPlanModal } from "./components/RevisionPlanModal.jsx";
import { TemplatePicker } from "./components/TemplatePicker.jsx";
import { TriagePanel } from "./components/TriagePanel.jsx";
import { ValidationPanel } from "./components/ValidationPanel.jsx";
import { WakingNotice } from "./components/WakingNotice.jsx";
import { WorkflowStepper } from "./components/WorkflowStepper.jsx";
import { RouteNotice } from "./components/RouteNotice.jsx";
import {
  CASE_SCOPED_MODES,
  MODES_WITH_NEW,
  canonicalPath,
  caseRouteAction,
  matterMatchesKey,
  parseLocation,
  pathForMode,
  paths,
  resumePath,
  routeCaseState,
  signInReturnPath,
} from "./routes/paths.js";
import { caseLookupStatus, initialActiveCase, rememberCase } from "./state/activeCase.js";
import { waitForDrafts } from "./state/draftJobs.js";
import { blockDefaultsApply, draftScreenFor, hydrateSavedSession, stepForView } from "./state/resumeWorkspace.js";
import { useSavedSessions } from "./hooks/useSavedSessions.js";
import { SavedSessionList } from "./components/SavedSessionList.jsx";
import { DraftJobProgress } from "./components/DraftJobProgress.jsx";
import { activeDraft, draftWorkspaceReducer, hasUnsavedDocuments, initialDraftWorkspace } from "./state/draftWorkspace.js";
import { checkpointFromWorkspace, planChanged, sessionChanges } from "./state/sessionCheckpoint.js";
import { leavesSavedWork, saveStatus } from "./state/unsavedGuard.js";
import { SaveStatusBar } from "./components/SaveStatusBar.jsx";
import { UnsavedChangesDialog } from "./components/UnsavedChangesDialog.jsx";
import { useModalDismiss } from "./hooks/useModalDismiss.js";

const BASE_WORKFLOW_STEPS = [
  { id: "goal", label: "Goal" },
  { id: "plan", label: "Plan" },
  { id: "editor", label: "Draft" },
];

// One screen of cases; "Show more" asks for the next page of the same size.
const CASE_PAGE_SIZE = 20;
// Open cases the advocate is working, most recently active first.
const DEFAULT_CASE_FILTERS = { status: "open", assigned: "all", problem: "", sort: "activity" };

const modeOptions = [
  { id: "case", label: "Case", icon: ClipboardList },
  { id: "triage", label: "Triage", icon: ClipboardCheck },
  { id: "case_chat", label: "Chat", icon: MessageSquare },
  { id: "research", label: "Research", icon: Search },
  { id: "advice_letter", label: "Advice letter", icon: Mail },
  { id: "draft", label: "Draft", icon: PenLine },
  { id: "template_fill", label: "Fill template", icon: PenLine },
  { id: "argument_gym", label: "Argument gym", icon: Swords },
];

export function App() {
  const [boot, setBoot] = useState(null);
  const [cases, setCases] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [triageRubrics, setTriageRubrics] = useState([]);
  // The URL says which screen and which case; see routes/paths.js.
  const location = useLocation();
  const navigate = useNavigate();
  const route = useMemo(() => parseLocation(location.pathname), [location.pathname]);
  const mode = route.mode;
  const [draftMode, setDraftMode] = useState("draft_from_template");
  // Unsaved setup keeps its step in memory; a saved session's step is its URL.
  const [localDraftStep, setDraftStep] = useState("goal");
  const draftStep = (route.mode === "draft" && stepForView(route.view)) || localDraftStep;
  const [draftGoal, setDraftGoal] = useState("");
  const [goalSuggestions, setGoalSuggestions] = useState([]);
  const [goalSuggestionGuidance, setGoalSuggestionGuidance] = useState("");
  const [goalSuggestionsBusy, setGoalSuggestionsBusy] = useState(false);
  const [selectedGoalSuggestionId, setSelectedGoalSuggestionId] = useState("");
  const [planningMode, setPlanningMode] = useState("suggest");
  const [allowMultipleDocuments, setAllowMultipleDocuments] = useState(false);
  const [clarifyMissingFactsBeforeDraft, setClarifyMissingFactsBeforeDraft] = useState(true);
  const [draftPlan, setDraftPlan] = useState(null);
  // The case being worked on. A URL that names a case sets it; screens whose
  // URL carries no case (research, the argument gym) keep it as context.
  const [activeCaseKey, setActiveCaseKey] = useState(null);
  const activeCaseKeyRef = useRef(null);
  activeCaseKeyRef.current = activeCaseKey;
  // The last route-key lookup: { key, status } -- see routeCaseState().
  const [caseLookup, setCaseLookup] = useState({ key: null, status: "idle" });
  // Bumped after connecting LegalServer, so a case that could not be reached
  // before is looked up again.
  const [caseLookupAttempt, setCaseLookupAttempt] = useState(0);
  const [matter, setMatter] = useState(null);
  const matterRef = useRef(null);
  matterRef.current = matter;
  const selectedMatterId = matter?.id ?? null;
  const caseState = routeCaseState(route, { matter, lookup: caseLookup });
  const savedDraftingSessions = useSavedSessions(
    route.mode === "draft" && route.view === null && caseState === "ready" ? matter?.routeCaseKey || route.caseKey : null,
  );
  const savedLetters = useSavedSessions(
    route.mode === "advice_letter" && route.view === null && caseState === "ready" ? matter?.routeCaseKey || route.caseKey : null,
    { workspace: "advice-letters" },
  );
  const [selectedTemplateId, setSelectedTemplateId] = useState(null);
  // The template list opens on a default so the picker is never empty, but a
  // default is not a choice: until the advocate picks one (or a draft exists)
  // the header must not announce a "selected document" on every case.
  const [templateChosen, setTemplateChosen] = useState(false);
  const [selectedFactIds, setSelectedFactIds] = useState([]);
  const [selectedCuratedFacts, setSelectedCuratedFacts] = useState([]);
  const [selectedBlockKeys, setSelectedBlockKeys] = useState([]);
  const [templateData, setTemplateData] = useState({});
  const [candidateIssues, setCandidateIssues] = useState([]);
  const [sourceResults, setSourceResults] = useState([]);
  const [session, setSession] = useState(null);
  const sessionRef = useRef(null);
  sessionRef.current = session;
  // The last saved-session lookup: { id, status, resume }.
  const [sessionLookup, setSessionLookup] = useState({ id: null, status: "idle", resume: null });
  // What a restored session selected, so template defaults do not replace it.
  const restoredSelectionRef = useRef(null);
  const defaultTemplateIdRef = useRef(null);
  const [jobProgress, setJobProgress] = useState({ jobId: null, status: "idle", error: "" });
  // Saving a session's choices: { saving, failed, error, conflict } where
  // conflict is the session as another window saved it.
  const [sessionSave, setSessionSave] = useState({ saving: false, failed: false, error: "", conflict: null });
  const [documentSave, setDocumentSave] = useState({ saving: false, failed: false, error: "" });
  const [leaveSave, setLeaveSave] = useState({ busy: false, error: "" });
  const [outline, setOutline] = useState(null);
  const [workspace, dispatchWorkspace] = useReducer(draftWorkspaceReducer, initialDraftWorkspace);
  const [revisionBusy, setRevisionBusy] = useState(false);
  const [gymFocusRun, setGymFocusRun] = useState(null);
  const [stressTestBusy, setStressTestBusy] = useState(false);
  const [triageAssessment, setTriageAssessment] = useState(null);
  const [triageDelivery, setTriageDelivery] = useState(null);
  const [saveDraftToLegalServer, setSaveDraftToLegalServer] = useState(true);
  const [draftDelivery, setDraftDelivery] = useState(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [triageHistory, setTriageHistory] = useState([]);
  const [selectedTriageRubricId, setSelectedTriageRubricId] = useState("");
  const [draftAuthorProfile, setDraftAuthorProfile] = useState(emptyAuthorProfile);
  const [instructions, setInstructions] = useState("");
  const [busy, setBusy] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [authBusy, setAuthBusy] = useState(false);
  const [auth, setAuth] = useState(null);
  const [legalserver, setLegalserver] = useState(null);
  const [legalserverIdentifier, setLegalserverIdentifier] = useState("");
  const [caseSearch, setCaseSearch] = useState("");
  const [caseFilters, setCaseFilters] = useState(DEFAULT_CASE_FILTERS);
  const caseRequestRef = useRef(0);
  const [caseListMeta, setCaseListMeta] = useState({ total: 0, hasMore: false, problemCodes: [] });
  const [caseBusy, setCaseBusy] = useState(false);
  const [manualCaseBusy, setManualCaseBusy] = useState(false);
  const [accountBusy, setAccountBusy] = useState(false);
  const [credentials, setCredentials] = useState({ username: "", secret: "" });
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [sourceDetailsOpen, setSourceDetailsOpen] = useState(false);
  const [connectionSettingsOpen, setConnectionSettingsOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [casePreviewMatterId, setCasePreviewMatterId] = useState(null);
  const [error, setError] = useState("");
  const profileModalRef = useRef(null);
  const connectionModalRef = useRef(null);
  useModalDismiss(profileModalRef, () => setProfileOpen(false), { active: profileOpen });
  useModalDismiss(connectionModalRef, () => setConnectionSettingsOpen(false), { active: connectionSettingsOpen });

  const drafts = workspace.drafts;
  const draft = activeDraft(workspace);
  const validationSummary = workspace.validationSummary;
  const draftDirtySinceValidation = workspace.dirtySinceValidation;
  const revisionPlan = workspace.revisionPlan;
  const casePreviewListMatter = cases.find((item) => item.id === casePreviewMatterId) || null;
  const casePreviewMatter = matter?.id === casePreviewMatterId ? matter : casePreviewListMatter;

  const pendingPlanQuestions = useMemo(() => planQuestionsForReview(draftPlan), [draftPlan]);
  const workflowSteps = useMemo(() => {
    if (!clarifyMissingFactsBeforeDraft || pendingPlanQuestions.length === 0) return BASE_WORKFLOW_STEPS;
    return [
      { id: "goal", label: "Goal" },
      { id: "plan", label: "Plan" },
      { id: "questions", label: "Answer questions" },
      { id: "editor", label: "Draft" },
    ];
  }, [clarifyMissingFactsBeforeDraft, pendingPlanQuestions.length]);

  useEffect(() => {
    async function load() {
      try {
        // Not plain api.me(): a replica that has just woken answers its first
        // requests with a 500, and treating that as "signed out" is what put
        // the login form in front of advocates whose session was perfectly
        // good. Ask again until the server actually says something.
        const authResponse = await retryWhileUnreachable(() => api.me());
        setAuth(authResponse.user);
        setDraftAuthorProfile({ ...emptyAuthorProfile, ...(authResponse.user.profile || {}) });
        if (authResponse.user.isAuthenticated) {
          await loadWorkspace();
        } else {
          setWorkspaceLoading(false);
        }
      } catch (err) {
        setError(err.message);
        setWorkspaceLoading(false);
      }
    }
    load();
  }, []);

  // Recover the documents a session already produced when the editor is opened
  // without them in memory, so a plan's later documents are never stranded.
  useEffect(() => {
    if (mode !== "draft" || draftStep !== "editor") return undefined;
    if (!session?.id || drafts.length > 0) return undefined;
    let cancelled = false;
    api.sessionDrafts(session.id)
      .then((response) => {
        if (!cancelled && response.drafts?.length) {
          dispatchWorkspace({ type: "documentsLoaded", drafts: response.drafts });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [mode, draftStep, session?.id, drafts.length]);

  async function loadWorkspace() {
    setWorkspaceLoading(true);
    try {
      const bootstrap = await api.bootstrap();
      const [caseResponse, templateResponse, rubricResponse] = await Promise.all([
        api.cases(),
        api.templates(),
        api.triageRubrics(),
      ]);
      setBoot(bootstrap);
      setSaveDraftToLegalServer(saveDefault(bootstrap.legalserverSave, "documents"));
      applyCaseResponse(caseResponse);
      setTemplates(templateResponse.templates);
      setTriageRubrics(rubricResponse.rubrics || []);
      setSelectedTriageRubricId((current) => current || rubricResponse.rubrics?.[0]?.id || "");
      const defaultTemplate = templateResponse.templates.find((item) => item.slug === "answer-counterclaims-cleveland");
      defaultTemplateIdRef.current = defaultTemplate?.id ?? templateResponse.templates[0]?.id ?? null;
      setSelectedTemplateId((current) => current ?? defaultTemplateIdRef.current);
    } finally {
      setWorkspaceLoading(false);
    }
  }

  function applyCaseResponse(caseResponse, { append = false } = {}) {
    const incoming = caseResponse.cases || [];
    setCases((current) => {
      if (!append) return incoming;
      const known = new Set(current.map((item) => item.id));
      return [...current, ...incoming.filter((item) => !known.has(item.id))];
    });
    setCaseListMeta({
      total: caseResponse.total ?? incoming.length,
      hasMore: Boolean(caseResponse.hasMore),
      problemCodes: caseResponse.problemCodes || [],
    });
    setLegalserver(caseResponse.legalserver || null);
    setLegalserverIdentifier(caseResponse.legalserver?.identifier || caseResponse.legalserver?.suggestedIdentifier || "");
    if (append) return;
    // A filter narrows what is listed, not what is being worked on: the active
    // case stays active even when the current filter would not show it. And
    // the first row is never activated for the advocate: that switched every
    // screen to a client nobody chose (see state/activeCase.js).
    if (!incoming.length && !selectedMatterId) {
      setMatter(null);
      setSelectedFactIds([]);
    }
  }

  async function loadCases({ query = caseSearch, filters = caseFilters, append = false } = {}) {
    // A round trip to LegalServer takes seconds, which is long enough for an
    // advocate to change a second filter before the first one answers. Without
    // this, the slower reply lands last and the list shows a filter nobody has
    // selected any more.
    const requestId = caseRequestRef.current + 1;
    caseRequestRef.current = requestId;
    setCaseBusy(true);
    setError("");
    try {
      const caseResponse = await api.cases({
        query: query.trim(),
        ...filters,
        limit: CASE_PAGE_SIZE,
        offset: append ? cases.length : 0,
      });
      if (caseRequestRef.current !== requestId) return;
      applyCaseResponse(caseResponse, { append });
    } catch (err) {
      if (caseRequestRef.current !== requestId) return;
      setError(err.message);
    } finally {
      if (caseRequestRef.current === requestId) setCaseBusy(false);
    }
  }

  async function applyCaseFilters(nextFilters) {
    setCaseFilters(nextFilters);
    await loadCases({ filters: nextFilters });
  }

  async function handleLogin(event) {
    event.preventDefault();
    setAuthBusy(true);
    setError("");
    try {
      // Same waking replica, same 500 -- and here it lands on someone who has
      // just typed their credentials, so failing it outright asks them to type
      // them a second time for no reason. A wrong secret answers 400 and is
      // reported at once.
      const response = await retryWhileUnreachable(
        () => api.login({ username: credentials.username, ["pass" + "word"]: credentials.secret }),
      );
      setAuth(response.user);
      setDraftAuthorProfile({ ...emptyAuthorProfile, ...(response.user.profile || {}) });
      setCredentials({ username: "", secret: "" });
      await loadWorkspace();
    } catch (err) {
      setError(err.message);
    } finally {
      setAuthBusy(false);
    }
  }

  async function handleLogout() {
    setAuthBusy(true);
    setError("");
    setAccountMenuOpen(false);
    try {
      await api.logout();
      const response = await api.me();
      // Nothing of this account's case survives into the next sign-in on this
      // tab; the URL stays, and is resolved again for whoever signs in.
      setActiveCaseKey(null);
      setMatter(null);
      setCaseLookup({ key: null, status: "idle" });
      setAuth(response.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setAuthBusy(false);
    }
  }

  function updateAuthProfile(profile) {
    setAuth((current) => current ? { ...current, profile, name: profile.displayName || current.name } : current);
    setDraftAuthorProfile((current) => ({ ...current, ...profile }));
  }

  async function handleOffice365Login() {
    setAuthBusy(true);
    setError("");
    try {
      // The page asked for comes back after sign-in; the server checks it.
      const response = await api.startOffice365Login(signInReturnPath(location.pathname));
      window.location.href = response.authUrl;
    } catch (err) {
      setError(err.message);
    } finally {
      setAuthBusy(false);
    }
  }

  // Restore the case this advocate last chose, once they are signed in. It
  // only fills a URL that names no case; a pasted link always wins.
  useEffect(() => {
    if (!auth?.username) return;
    setActiveCaseKey((current) => initialActiveCase({ current, username: auth.username }));
  }, [auth?.username]);

  const routeAction = useMemo(() => caseRouteAction(route, { activeCaseKey }), [route, activeCaseKey]);
  const loadCaseKey = routeAction.type === "load" ? routeAction.caseKey : null;

  // A task URL that names no case, while one is active, is rewritten to name
  // it -- replaced, so Back does not return to the caseless address.
  useEffect(() => {
    if (!auth?.isAuthenticated || routeAction.type !== "redirect") return;
    navigate(routeAction.to, { replace: true });
  }, [auth?.isAuthenticated, routeAction, navigate]);

  // Open the case the URL names. Read-only: resolving a link never runs,
  // saves, or generates anything.
  useEffect(() => {
    if (!auth?.isAuthenticated) return undefined;
    if (!loadCaseKey) {
      setCaseLookup({ key: null, status: "idle" });
      setMatter(null);
      setSelectedFactIds([]);
      setSelectedCuratedFacts([]);
      return undefined;
    }
    if (matterMatchesKey(matterRef.current, loadCaseKey)) {
      setCaseLookup({ key: loadCaseKey, status: "ready" });
      return undefined;
    }
    // Let go of the previous case at once. The next one takes seconds to load
    // from LegalServer, and until then every screen still acted on the old
    // client: "Make active" then "Make plan" drafted for the case just left.
    setMatter(null);
    setSelectedFactIds([]);
    setSelectedCuratedFacts([]);
    setCaseLookup({ key: loadCaseKey, status: "loading" });
    const controller = new AbortController();
    // A link opened cold may be the first thing to wake the server, which
    // answers its first requests with a 500; that is worth asking again about,
    // not a reason to call the case unavailable.
    retryWhileUnreachable(() => api.caseByRouteKey(loadCaseKey, { signal: controller.signal }))
      .then((response) => {
        if (controller.signal.aborted) return;
        const loaded = response.case;
        setMatter(loaded);
        setSelectedFactIds(loaded.facts.filter((fact) => fact.selectedByDefault).map((fact) => fact.id));
        setSelectedCuratedFacts([]);
        setActiveCaseKey(loaded.routeCaseKey || loaded.id);
        setCaseLookup({ key: loadCaseKey, status: "ready" });
      })
      .catch((err) => {
        if (controller.signal.aborted || err?.name === "AbortError") return;
        setMatter(null);
        setSelectedFactIds([]);
        setSelectedCuratedFacts([]);
        const status = caseLookupStatus(err);
        if (status === "not_connected" || status === "identity_mismatch") {
          // Nothing is wrong with the case; the account cannot reach it yet.
          setCaseLookup({ key: loadCaseKey, status });
          return;
        }
        if (status === "unavailable") {
          setCaseLookup({ key: loadCaseKey, status: "unavailable" });
          if (loadCaseKey === activeCaseKeyRef.current) {
            // The remembered case no longer opens: forget it, don't retry it.
            // A bad pasted link leaves the case being worked on alone.
            rememberCase(auth?.username, null);
            setActiveCaseKey(null);
          }
          return;
        }
        setCaseLookup({ key: loadCaseKey, status: "error" });
        setError(err.message);
      });
    return () => controller.abort();
  }, [auth?.isAuthenticated, auth?.username, loadCaseKey, caseLookupAttempt]);

  // An old case number or a bare external id opens the case, then the address
  // is replaced with the link the server gives now.
  useEffect(() => {
    if (!matter || !route.caseKey || caseLookup.status !== "ready" || caseLookup.key !== route.caseKey) return;
    if (!matter.routeCaseKey || matter.routeCaseKey === route.caseKey) return;
    const to = canonicalPath(route, matter.routeCaseKey);
    if (to) navigate(to, { replace: true });
  }, [matter, route, caseLookup, navigate]);

  useEffect(() => {
    if (auth?.username && selectedMatterId) rememberCase(auth.username, selectedMatterId);
  }, [auth?.username, selectedMatterId]);

  // Put a saved session back on screen exactly as it was saved.
  async function refreshSession(sessionId) {
    try {
      const detail = await api.savedSession(sessionId);
      // Only called once everything was saved (generation saves first), so
      // the server's copy -- which generation may have added sources to -- is
      // what the screen should show.
      if (sessionRef.current?.id === sessionId) applySavedSession(detail.session);
    } catch {
      // A stale revision only means the next save asks; nothing is lost.
    }
  }

  // `savedDrafts` null restores the session's choices and leaves the
  // documents on screen alone.
  function applySavedSession(saved, savedDrafts = null) {
    const snapshot = hydrateSavedSession(saved, { defaultTemplateId: defaultTemplateIdRef.current });
    setSession(saved);
    setDraftMode(snapshot.draftMode);
    setDraftGoal(snapshot.draftGoal);
    setInstructions(snapshot.instructions);
    setPlanningMode(snapshot.planningMode);
    setAllowMultipleDocuments(snapshot.allowMultipleDocuments);
    setClarifyMissingFactsBeforeDraft(snapshot.clarifyMissingFactsBeforeDraft);
    setSelectedTemplateId(snapshot.selectedTemplateId);
    setTemplateChosen(snapshot.templateChosen);
    setSelectedFactIds(snapshot.selectedFactIds);
    setSelectedCuratedFacts(snapshot.selectedCuratedFacts);
    setSelectedBlockKeys(snapshot.selectedBlockKeys);
    setTemplateData(snapshot.templateData);
    setSourceResults(snapshot.sourceResults);
    setDraftPlan(snapshot.draftPlan);
    setGoalSuggestions([]);
    setSelectedGoalSuggestionId("");
    setOutline(null);
    if (snapshot.authorProfile) setDraftAuthorProfile((current) => ({ ...current, ...snapshot.authorProfile }));
    restoredSelectionRef.current = {
      matterId: saved.matter?.id,
      selectedFactIds: snapshot.selectedFactIds,
      templateId: snapshot.selectedTemplateId == null ? null : Number(snapshot.selectedTemplateId),
    };
    setSessionSave({ saving: false, failed: false, error: "", conflict: null });
    if (savedDrafts === null) return;
    dispatchWorkspace({ type: "reset" });
    dispatchWorkspace({ type: "documentsLoaded", drafts: savedDrafts });
  }

  // Open the saved session a drafting URL names, once its case has opened.
  // Two reads -- the session and its documents -- and nothing else: no plan,
  // no generation, no validation. /sessions/<id> then moves on to the
  // furthest point the session saved, replacing itself in history.
  const routeSessionId = route.mode === "draft" ? route.sessionId : null;
  const resolvingSession = route.mode === "draft" && route.view === "session";
  // Held in memory already -- and on this URL's case, or it is not the same
  // thing at all: a session from another case never skips the server's check.
  const holdsRouteSession = (held) => Boolean(held) && held.id === routeSessionId && held.matter?.id != null && held.matter.id === matterRef.current?.id;
  useEffect(() => {
    if (!auth?.isAuthenticated || !routeSessionId || caseState !== "ready") return undefined;
    if (!resolvingSession && holdsRouteSession(sessionRef.current)) return undefined;
    const controller = new AbortController();
    const caseKey = route.caseKey;
    setSessionLookup({ id: routeSessionId, status: "loading", resume: null });
    retryWhileUnreachable(() => Promise.all([
      api.savedSession(routeSessionId, { caseKey, workspace: "drafting" }, { signal: controller.signal }),
      api.sessionDrafts(routeSessionId, { signal: controller.signal }),
    ]))
      .then(([detail, draftResponse]) => {
        if (controller.signal.aborted) return;
        if (!holdsRouteSession(sessionRef.current)) applySavedSession(detail.session, draftResponse.drafts || []);
        setSessionLookup({ id: routeSessionId, status: "ready", resume: detail.resume });
        if (resolvingSession) navigate(resumePath(caseKey, routeSessionId, detail.resume), { replace: true });
      })
      .catch((err) => {
        if (controller.signal.aborted || err?.name === "AbortError") return;
        setSessionLookup({ id: routeSessionId, status: err.status === 404 ? "unavailable" : "error", resume: null });
        if (err.status !== 404) setError(err.message);
      });
    return () => controller.abort();
    // route.caseKey is read, not watched: rewriting an old case number to the
    // current one names the same session and must not reload it.
  }, [auth?.isAuthenticated, routeSessionId, resolvingSession, caseState]);

  // The document a URL names is the one on screen.
  const routeDraftId = route.mode === "draft" ? route.draftId : null;
  useEffect(() => {
    if (!routeDraftId || workspace.activeDraftId === routeDraftId) return;
    if (workspace.drafts.some((item) => item.id === routeDraftId)) {
      dispatchWorkspace({ type: "documentSelected", draftId: routeDraftId });
    }
  }, [routeDraftId, workspace.activeDraftId, workspace.drafts]);

  // /jobs/<id> reconnects to a generation already running. It polls the job it
  // names; it never starts one, so a reload cannot make a second draft.
  const routeJobId = route.mode === "draft" && route.view === "job" ? route.jobId : null;
  const sessionInMemory = caseState === "ready" && holdsRouteSession(session);

  // What on screen is not saved yet. Tracked only for a saved session: setup
  // at /new is unsaved by definition and says so.
  const workspaceSnapshot = {
    draftGoal, instructions, selectedFactIds, selectedCuratedFacts, sourceResults, selectedBlockKeys,
    planningMode, selectedTemplateId, templateData, allowMultipleDocuments, clarifyMissingFactsBeforeDraft,
  };
  const changedSessionFields = sessionInMemory
    ? sessionChanges(session, workspaceSnapshot, { defaultTemplateId: defaultTemplateIdRef.current })
    : [];
  const planDirty = sessionInMemory && planChanged(session, draftPlan);
  const sessionDirty = changedSessionFields.length > 0 || planDirty;
  const documentsDirty = sessionInMemory && hasUnsavedDocuments(workspace);
  // Screens that keep their own edits (the advice letter) report them here and
  // register how to save them, so leaving asks the same way everywhere.
  const [panelDirty, setPanelDirty] = useState(false);
  const panelSaveRef = useRef(null);
  const hasUnsavedWork = sessionDirty || documentsDirty || panelDirty;

  // Save the session's choices and plan, if changed. Resolves to the saved
  // session, or null when the save failed -- and a caller about to generate
  // must stop on null rather than draft from what the server does not have.
  async function saveSessionChanges() {
    const current = sessionRef.current;
    if (!current || (!changedSessionFields.length && !planDirty)) return current;
    setSessionSave({ saving: true, failed: false, error: "", conflict: null });
    try {
      let saved = current;
      if (changedSessionFields.length) {
        const response = await api.checkpointSession(current.id, { revision: current.revision, ...checkpointFromWorkspace(workspaceSnapshot) });
        saved = response.session;
      }
      if (planDirty) {
        const response = await api.updateDraftPlan(saved.id, { draftPlan, goal: draftGoal, revision: saved.revision });
        saved = response.session;
        setDraftPlan(response.plan);
      }
      setSession(saved);
      setSessionSave({ saving: false, failed: false, error: "", conflict: null });
      return saved;
    } catch (err) {
      if (isConflict(err)) setSessionSave({ saving: false, failed: false, error: "", conflict: err.data.session });
      else setSessionSave({ saving: false, failed: true, error: err.message, conflict: null });
      return null;
    }
  }

  function resolveSessionConflict(keep) {
    const theirs = sessionSave.conflict;
    if (!theirs) return;
    if (keep === "theirs") {
      applySavedSession(theirs);
      setDraftPlan(theirs.draftPlan && Object.keys(theirs.draftPlan).length ? theirs.draftPlan : null);
      return;
    }
    // Keep mine: compare against what they saved, so the next save
    // deliberately replaces it with what is on screen.
    setSession(theirs);
    setSessionSave({ saving: false, failed: false, error: "", conflict: null });
  }

  // Save one document's edits against the revision they were made on.
  async function saveDraftDocument(target) {
    if (!target) return true;
    setDocumentSave({ saving: true, failed: false, error: "" });
    try {
      const response = await api.updateDraft(target.id, {
        sections: target.sections,
        plainText: target.plainText,
        editorState: target.editorState,
        revision: target.revision,
      });
      dispatchWorkspace({ type: "documentEdited", draft: response.draft });
      setDocumentSave({ saving: false, failed: false, error: "" });
      return response.draft;
    } catch (err) {
      if (isConflict(err)) {
        dispatchWorkspace({ type: "documentConflict", draft: err.data.draft });
        setDocumentSave({ saving: false, failed: false, error: "" });
      } else {
        setDocumentSave({ saving: false, failed: true, error: err.message });
      }
      return null;
    }
  }

  function resolveDocumentConflict(keep) {
    const conflict = workspace.conflict;
    if (!conflict) return;
    dispatchWorkspace({ type: "conflictResolved", keep });
    if (keep === "mine") {
      const local = workspace.drafts.find((item) => item.id === conflict.draftId);
      if (local) saveDraftDocument({ ...local, revision: conflict.server.revision });
    }
  }

  async function saveAllWork() {
    if (panelDirty && panelSaveRef.current && !(await panelSaveRef.current())) return false;
    for (const draftId of workspace.unsavedDraftIds) {
      const target = workspace.drafts.find((item) => item.id === draftId);
      if (target && !(await saveDraftDocument(target))) return false;
    }
    return Boolean(await saveSessionChanges());
  }

  // Leaving saved work with unsaved changes asks first. Moving between the
  // steps and documents of the same session keeps everything and never asks.
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    hasUnsavedWork && leavesSavedWork(currentLocation.pathname, nextLocation.pathname)
  ));
  useEffect(() => {
    if (!hasUnsavedWork) return undefined;
    const warn = (event) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasUnsavedWork]);

  async function saveAndLeave() {
    setLeaveSave({ busy: true, error: "" });
    const ok = await saveAllWork();
    if (!ok) {
      setLeaveSave({ busy: false, error: sessionSave.error || documentSave.error || "Another window saved first; resolve that before leaving." });
      return;
    }
    setLeaveSave({ busy: false, error: "" });
    blocker.proceed?.();
  }

  function discardAndLeave() {
    // Drop the edited copy; coming back reloads the session as saved.
    setSession(null);
    setDraftPlan(null);
    restoredSelectionRef.current = null;
    dispatchWorkspace({ type: "reset" });
    setPanelDirty(false);
    setLeaveSave({ busy: false, error: "" });
    blocker.proceed?.();
  }
  useEffect(() => {
    if (!routeJobId || !sessionInMemory) return undefined;
    let cancelled = false;
    const caseKey = route.caseKey;
    const sessionId = routeSessionId;
    const stop = new Error("stopped");
    const sleep = (ms) => new Promise((resolve, reject) => setTimeout(() => (cancelled ? reject(stop) : resolve()), ms));
    const poll = (jobId) => api.draftGenerationJob(sessionId, jobId);
    setJobProgress({ jobId: routeJobId, status: "pending", error: "" });
    poll(routeJobId)
      .then((first) => waitForDrafts(first, poll, {
        sleep,
        onProgress: (job) => { if (!cancelled) setJobProgress({ jobId: routeJobId, status: job.status, error: "" }); },
      }))
      .then((generated) => {
        if (cancelled) return;
        dispatchWorkspace({ type: "documentsGenerated", drafts: generated });
        setJobProgress({ jobId: routeJobId, status: "complete", error: "" });
        // Generation saves the session too; pick up its new revision so the
        // next save is not refused as if another window had made it.
        refreshSession(sessionId);
        const first = generated[0]?.id;
        navigate(first ? paths.draft(caseKey, sessionId, first) : paths.draftingSessionView(caseKey, sessionId, "plan"), { replace: true });
      })
      .catch((err) => {
        if (cancelled || err === stop) return;
        setJobProgress({ jobId: routeJobId, status: "failed", error: err.status === 404 ? "This generation is not part of this session." : err.message });
      });
    return () => { cancelled = true; };
  }, [routeJobId, routeSessionId, sessionInMemory]);

  // Arriving at /drafting/<case>/new is arriving at setup, for something new:
  // a saved session still in memory is let go, so nothing done here can act on
  // it. What the advocate typed is kept; only the saved work is detached.
  useEffect(() => {
    if (route.mode !== "draft" || route.view !== "new") return;
    setDraftStep("goal");
    if (sessionRef.current) {
      setSession(null);
      setDraftPlan(null);
      setOutline(null);
      restoredSelectionRef.current = null;
      dispatchWorkspace({ type: "reset" });
    }
  }, [location.key, route.mode, route.view]);

  // The case's saved assessments. Reading them never runs triage.
  const [triageHistoryFor, setTriageHistoryFor] = useState(null);
  useEffect(() => {
    if (!auth?.isAuthenticated || !selectedMatterId) {
      setTriageAssessment(null);
      setTriageHistory([]);
      setTriageHistoryFor(null);
      return;
    }
    setTriageHistoryFor(null);
    api.caseTriage(selectedMatterId)
      .then((response) => {
        setTriageHistory(response.assessments || []);
        setTriageAssessment(response.assessments?.[0] || null);
        setTriageHistoryFor(selectedMatterId);
      })
      .catch(() => {
        setTriageHistory([]);
        setTriageAssessment(null);
        setTriageHistoryFor(selectedMatterId);
      });
  }, [auth, selectedMatterId]);

  async function handleLegalServerConnect(event) {
    event.preventDefault();
    setAccountBusy(true);
    setError("");
    try {
      const response = await api.connectLegalServer({ identifier: legalserverIdentifier });
      setLegalserver(response.legalserver);
      await loadWorkspace();
      setConnectionSettingsOpen(false);
      // A case this link could not reach before may open now.
      setCaseLookupAttempt((current) => current + 1);
    } catch (err) {
      setError(err.message);
    } finally {
      setAccountBusy(false);
    }
  }

  async function handleLegalServerDisconnect() {
    setAccountBusy(true);
    setError("");
    try {
      const response = await api.disconnectLegalServer();
      setLegalserver(response.legalserver);
      // The cases this account could open may have just changed.
      setActiveCaseKey(null);
      setMatter(null);
      navigate(paths.cases());
      await loadWorkspace();
    } catch (err) {
      setError(err.message);
    } finally {
      setAccountBusy(false);
    }
  }

  async function handleCaseSearch(event) {
    event.preventDefault();
    await loadCases({ query: caseSearch });
  }

  async function handleCaseSearchReset() {
    setCaseSearch("");
    await loadCases({ query: "", filters: DEFAULT_CASE_FILTERS });
    setCaseFilters(DEFAULT_CASE_FILTERS);
  }

  async function handleCreateManualCase(payload) {
    setManualCaseBusy(true);
    setError("");
    try {
      const formData = new FormData();
      formData.append("clientName", payload.clientName || "");
      formData.append("matterType", payload.matterType || "");
      formData.append("jurisdiction", payload.jurisdiction || "");
      formData.append("posture", payload.posture || "");
      formData.append("notes", payload.notes || "");
      (payload.files || []).forEach((file) => formData.append("files", file));
      const response = await api.createManualCase(formData);
      setCases((current) => {
        const withoutDuplicate = current.filter((item) => item.id !== response.case.id);
        return [response.case, ...withoutDuplicate];
      });
      setMatter(response.case);
      setSelectedFactIds((response.created || []).map((fact) => fact.id));
      setSelectedCuratedFacts([]);
      openCase(response.case);
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    } finally {
      setManualCaseBusy(false);
    }
  }

  async function handleUpdateManualCase(payload) {
    if (!matter) return false;
    setManualCaseBusy(true);
    setError("");
    try {
      const response = await api.updateManualCase(matter.id, payload);
      setMatter(response.case);
      setCases((current) => current.map((item) => item.id === response.case.id ? response.case : item));
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    } finally {
      setManualCaseBusy(false);
    }
  }

  async function exportDraftDocument(draftDocument) {
    if (!draftDocument) return;
    setExportBusy(true);
    setError("");
    setDraftDelivery(null);
    try {
      const response = await api.exportDraft(draftDocument.id, { saveToLegalServer: saveDraftToLegalServer });
      await saveResponseAsFile(response, `draft-${draftDocument.id}.${draftDocument.exportFormat || "docx"}`);
      setDraftDelivery(deliveryFromHeaders(response));
    } catch (err) {
      setError(err.message);
    } finally {
      setExportBusy(false);
    }
  }

  async function runTriage(rubricId = selectedTriageRubricId, { saveToLegalServer = false } = {}) {
    if (!matter) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.runCaseTriage(matter.id, { rubricId, saveToLegalServer });
      setTriageAssessment(response.assessment);
      setTriageDelivery(response.legalserver || null);
      setTriageHistory((current) => [response.assessment, ...current.filter((item) => item.id !== response.assessment.id)]);
      // The new assessment has its own address; reopening it never reruns.
      const caseKey = matter.routeCaseKey || matter.id;
      navigate(paths.triageAssessment(caseKey, response.assessment.id));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const selectedTemplate = useMemo(
    () => templates.find((template) => template.id === Number(selectedTemplateId)) || null,
    [selectedTemplateId, templates],
  );
  const selectedDraftDocument =
    draftMode === "draft_from_template" && (templateChosen || drafts.length > 0) ? selectedTemplate : null;

  function selectDraftTemplate(templateId) {
    setSelectedTemplateId(templateId);
    setTemplateChosen(Boolean(templateId));
    setTemplateData({});
  }

  useEffect(() => {
    if (!selectedTemplate) return;
    const inputs = { matterId: matter?.id, selectedFactIds, templateId: selectedTemplate.id };
    if (!blockDefaultsApply(restoredSelectionRef.current, inputs)) return;
    restoredSelectionRef.current = null;
    const keys = selectedTemplate.blocks
      .filter((block) => block.required || block.selectionRule?.fact_slugs?.some((slug) => matter?.facts?.find((fact) => fact.slug === slug && selectedFactIds.includes(fact.id))))
      .map((block) => block.key);
    setSelectedBlockKeys(keys);
  }, [matter, selectedFactIds, selectedTemplate]);

  async function startSession(nextStatus = "facts_review") {
    if (!matter) return null;
    setBusy(true);
    setError("");
    try {
      const payload = {
        mode: draftMode,
        matterId: matter.id,
        templateId: draftMode === "draft_from_scratch" ? undefined : selectedTemplateId,
        selectedTemplateIds: planningMode === "known" && selectedTemplateId ? [Number(selectedTemplateId)] : [],
        authorProfile: draftAuthorProfile,
        templateData,
        goal: draftGoal,
        instructions: instructions || draftGoal,
      };
      const response = await api.createSession(payload);
      const created = response.session;
      const advanced = await api.advanceSession(created.id, {
        status: nextStatus,
        selectedFactIds,
        selectedCuratedFacts,
        selectedSourceResults: sourceResults,
        selectedBlockKeys,
        authorProfile: draftAuthorProfile,
        templateData,
        goal: draftGoal,
        instructions: instructions || draftGoal,
        ...(draftMode === "draft_from_template" ? { template: selectedTemplateId } : {}),
      });
      setSession(advanced.session);
      setSelectedFactIds(advanced.session.selectedFactIds || []);
      setSelectedBlockKeys(advanced.session.selectedBlockKeys || []);
      return advanced.session;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function ensureGoalSuggestionSession() {
    if (session?.id) return session;
    if (!matter) return null;

    const response = await api.createSession({
      mode: draftMode,
      matterId: matter.id,
      templateId: draftMode === "draft_from_scratch" ? undefined : selectedTemplateId,
      selectedTemplateIds: planningMode === "known" && selectedTemplateId ? [Number(selectedTemplateId)] : [],
      authorProfile: draftAuthorProfile,
      templateData,
      goal: draftGoal,
      instructions: instructions || draftGoal,
    });
    setSession(response.session);
    setSelectedFactIds(response.session.selectedFactIds || selectedFactIds);
    setSelectedBlockKeys(response.session.selectedBlockKeys || selectedBlockKeys);
    return response.session;
  }

  async function suggestDraftGoals() {
    if (!matter) return;
    setGoalSuggestionsBusy(true);
    setError("");

    try {
      const activeSession = await ensureGoalSuggestionSession();
      if (!activeSession?.id) return;

      const response = await api.recommendSessionGoals(activeSession.id, { limit: 5 });
      setSession(response.session || activeSession);
      setGoalSuggestions(response.goals || []);
      setGoalSuggestionGuidance(response.guidance || "");

      const first = response.goals?.[0];
      if (first && !draftGoal.trim()) {
        setSelectedGoalSuggestionId(first.id);
        setDraftGoal(first.goal || "");
        setInstructions(first.instructions || first.goal || "");
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setGoalSuggestionsBusy(false);
    }
  }

  function selectGoalSuggestion(suggestion) {
    setSelectedGoalSuggestionId(suggestion.id);
    setDraftGoal(suggestion.goal || "");
    setInstructions(suggestion.instructions || suggestion.goal || "");
    if (suggestion.templateIds?.length) {
      setPlanningMode("known");
      setSelectedTemplateId(suggestion.templateIds[0]);
      setTemplateChosen(true);
    }
  }

  async function saveWorkflow(status, overrides = {}) {
    if (!session) return await startSession(status);
    setBusy(true);
    try {
      const response = await api.advanceSession(session.id, {
        status,
        selectedFactIds: overrides.selectedFactIds || selectedFactIds,
        selectedCuratedFacts: overrides.selectedCuratedFacts || selectedCuratedFacts,
        selectedSourceResults: overrides.selectedSourceResults || sourceResults,
        selectedBlockKeys: overrides.selectedBlockKeys || selectedBlockKeys,
        authorProfile: draftAuthorProfile,
        templateData,
        goal: draftGoal,
        instructions: instructions || draftGoal,
        draftPlan,
        selectedTemplateIds: planningMode === "known" && selectedTemplateId ? [Number(selectedTemplateId)] : [],
        ...(draftMode === "draft_from_template" ? { template: selectedTemplateId } : {}),
      });
      setSession(response.session);
      return response.session;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function makeDraftPlan(extraGuidance = "") {
    if (!matter) return;
    setBusy(true);
    setError("");
    try {
      const payload = {
        mode: "draft_from_template",
        matterId: matter.id,
        goal: draftGoal,
        instructions: [draftGoal, extraGuidance].filter(Boolean).join("\n\n"),
        authorProfile: draftAuthorProfile,
        templateData,
        selectedFactIds,
        selectedCuratedFacts,
        selectedSourceResults: sourceResults,
        selectedTemplateIds: planningMode === "known" && selectedTemplateId ? [Number(selectedTemplateId)] : [],
        allowMultipleDocuments,
      };
      const created = session || (await api.createSession({
        mode: "draft_from_template",
        matterId: matter.id,
        goal: draftGoal,
        instructions: draftGoal,
        authorProfile: draftAuthorProfile,
        templateData,
        selectedTemplateIds: payload.selectedTemplateIds,
      })).session;
      const response = await api.generateDraftPlan(created.id, payload);
      let plannedSession = response.session;
      if (!selectedFactIds.length) {
        const factResponse = await api.recommendSessionFacts(response.session.id, { apply: true });
        const recommendation = factRecommendationState(factResponse, response.session);
        plannedSession = recommendation.session;
        if (recommendation.matter) setMatter(recommendation.matter);
        setSelectedFactIds(recommendation.factIds);
      }
      // Record how planning was set up, so reopening asks the same way.
      try {
        const recorded = await api.checkpointSession(plannedSession.id, {
          revision: plannedSession.revision,
          workflowOptions: { planningMode, allowMultipleDocuments, clarifyMissingFactsBeforeDraft },
        });
        if (recorded?.session) plannedSession = recorded.session;
      } catch {
        // The plan exists either way; the options are a convenience on reopen.
      }
      setSession(plannedSession);
      setDraftPlan(response.plan);
      setSelectedBlockKeys(plannedSession.selectedBlockKeys || selectedBlockKeys);
      showDraftStep("plan", plannedSession);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveDraftPlan(plan = draftPlan) {
    if (!session?.id || !plan) return null;
    setBusy(true);
    setError("");
    try {
      const response = await api.updateDraftPlan(session.id, { draftPlan: plan, goal: draftGoal, revision: session.revision });
      setSession(response.session);
      setDraftPlan(response.plan);
      return response;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function regenerateDraftPlan(guidance = "") {
    await makeDraftPlan(guidance);
  }

  async function generateDraftsFromPlan() {
    // Generation drafts from what the server has. If the plan or choices on
    // screen could not be saved, generating would draft from something else.
    const activeSession = await saveSessionChanges();
    if (!activeSession?.id) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.generatePlanDrafts(activeSession.id, {
        requireAllMissingInformation: clarifyMissingFactsBeforeDraft,
        // One key per click: if the answer is lost and this is retried, the
        // server returns the same generation instead of starting a second.
        idempotencyKey: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
      });
      const current = parseLocation(window.location.pathname);
      if (response.job && !response.drafts && current.mode === "draft" && current.caseKey) {
        // The job has a URL: a reload or a second tab reconnects to it
        // instead of pressing Generate again and making a second draft.
        navigate(paths.draftingJob(current.caseKey, activeSession.id, response.job.id));
        return;
      }
      const drafts = await waitForDrafts(response, (jobId) => api.draftGenerationJob(activeSession.id, jobId));
      dispatchWorkspace({ type: "documentsGenerated", drafts });
      await refreshSession(activeSession.id);
      showDraftStep("editor", activeSession, drafts[0]?.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function goToQuestionsOrGenerate() {
    if (clarifyMissingFactsBeforeDraft && planQuestionsForReview(draftPlan).length > 0) {
      showDraftStep("questions");
      return;
    }
    generateDraftsFromPlan();
  }

  async function continueToFactReview() {
    const activeSession = session || await startSession("facts_review");
    if (!activeSession) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.recommendSessionFacts(activeSession.id, { apply: true });
      const recommendation = factRecommendationState(response, activeSession);
      setSession(recommendation.session);
      if (recommendation.matter) setMatter(recommendation.matter);
      setSelectedFactIds(recommendation.factIds);
      showDraftStep("facts");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function continueFromFactReview() {
    await saveWorkflow("support_review");
    showDraftStep("support");
  }

  async function continueFromDraftSupport() {
    await saveWorkflow("law_review");
    showDraftStep("law");
  }

  async function continueFromLawReview() {
    const approvedBlockKeys = candidateIssues
      .filter((issue) => issue.status === "approved")
      .flatMap((issue) => issue.outputs?.activate_blocks_after_approval || []);
    let nextSelectedBlockKeys = selectedBlockKeys;
    if (approvedBlockKeys.length) {
      nextSelectedBlockKeys = [...new Set([...selectedBlockKeys, ...approvedBlockKeys])];
      setSelectedBlockKeys(nextSelectedBlockKeys);
    }
    const activeSession = await saveWorkflow("outline_review", { selectedBlockKeys: nextSelectedBlockKeys });
    if (!activeSession) return;
    try {
      const response = await api.sessionOutline(activeSession.id);
      setOutline(response.outline);
      setSession(response.session || activeSession);
    } catch (err) {
      setError(err.message);
    }
    showDraftStep("outline");
  }

  async function approveOutline() {
    const activeSession = session || await startSession("outline_review");
    if (!activeSession) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.approveSessionOutline(activeSession.id, { selectedBlockKeys });
      setOutline(response.outline);
      setSession(response.session || activeSession);
      showDraftStep("editor");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function generateDraft() {
    const activeSession = session || await startSession("draft_review");
    if (!activeSession) return;
    setBusy(true);
    try {
      await api.advanceSession(activeSession.id, {
        status: "draft_review",
        selectedFactIds,
        selectedCuratedFacts,
        selectedSourceResults: sourceResults,
        selectedBlockKeys,
        authorProfile: draftAuthorProfile,
        templateData,
        instructions,
        ...(draftMode === "draft_from_template" ? { template: selectedTemplateId } : {}),
      });
      const response = await api.generateDraft(activeSession.id);
      dispatchWorkspace({ type: "documentsGenerated", drafts: response.draft ? [response.draft] : [] });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function validateDraft() {
    if (!draft) return;
    setBusy(true);
    try {
      const response = await api.validateDraft(draft.id);
      dispatchWorkspace({ type: "documentValidated", draft: response.draft, validation: response.validation });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function stressTestDraft() {
    if (!draft) return;
    setStressTestBusy(true);
    setError("");
    try {
      // The gym reads the stored document, so the editor's current text is
      // persisted first rather than being silently left out of the run.
      // A refused save (another window saved first) stops here: stress
      // testing text other than what is on screen would mislead.
      if (!(await saveDraftDocument(draft))) return;
      const response = await api.stressTestDraft(draft.id);
      setGymFocusRun({ run: response.run, workspace: response.workspace });
      goToMode("argument_gym");
    } catch (err) {
      setError(err.message);
    } finally {
      setStressTestBusy(false);
    }
  }

  async function regenerateDraftBlock(blockKey, instruction = "") {
    if (!draft) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.regenerateDraftBlock(draft.id, blockKey, { instruction });
      dispatchWorkspace({ type: "documentEdited", draft: response.draft });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function fillMissingField(fieldKey, value, sections, plainText, editorState) {
    setBusy(true);
    setError("");
    try {
      if (session?.id && fieldKey) {
        const sessionResponse = await api.updateSessionTemplateData(session.id, { [fieldKey]: value });
        setSession(sessionResponse.session);
        setTemplateData(sessionResponse.session.templateData);
      }
      if (draft) {
        await saveDraftDocument({ ...draft, sections, plainText, editorState });
      } else {
        dispatchWorkspace({ type: "documentEdited" });
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function openRevisionPlan() {
    if (!draft) return;
    setRevisionBusy(true);
    setError("");
    try {
      const response = await api.draftRevisionPlan(draft.id);
      dispatchWorkspace({ type: "revisionPlanLoaded", plan: response.revisionPlan });
    } catch (err) {
      setError(err.message);
    } finally {
      setRevisionBusy(false);
    }
  }

  function updateRevisionPlanItem(blockKey, patch) {
    dispatchWorkspace({ type: "revisionPlanItemUpdated", blockKey, patch });
  }

  async function applyRevisionPlan() {
    if (!draft || !revisionPlan) return;
    setRevisionBusy(true);
    setError("");
    try {
      const response = await api.applyDraftRevision(draft.id, revisionPlan.plan);
      dispatchWorkspace({ type: "revisionPlanApplied", draft: response.draft, validation: response.validation });
    } catch (err) {
      setError(err.message);
    } finally {
      setRevisionBusy(false);
    }
  }

  // Every screen change is a navigation, so the address bar always says where
  // the advocate is. Drafting opens on setup, as the sidebar always has.
  function goToMode(nextMode, caseKey = matter?.routeCaseKey || activeCaseKey) {
    if (nextMode === "draft") setDraftStep("goal");
    navigate(pathForMode(nextMode, caseKey, { view: MODES_WITH_NEW.has(nextMode) ? "new" : null }));
  }

  // Make a case the active one, on the given screen or the current one. A
  // screen whose URL carries no case keeps its address and takes the case as
  // context.
  function openCase(caseLike, nextMode = mode) {
    if (!caseLike) return;
    const caseKey = caseLike.routeCaseKey || caseLike.id;
    setActiveCaseKey(caseKey);
    const target = nextMode || "case";
    if (CASE_SCOPED_MODES.has(target) || target !== mode) goToMode(target, caseKey);
  }

  function selectCaseById(matterId) {
    openCase(cases.find((item) => item.id === matterId) || (matter?.id === matterId ? matter : { id: matterId }));
  }

  // Move the drafting workflow to a step. Once a session is saved, a step is a
  // URL; leaving setup replaces /new, because setup has just become that saved
  // session and Back should not offer to start it again.
  function showDraftStep(step, activeSession = sessionRef.current, draftId = null) {
    const current = parseLocation(window.location.pathname);
    const sessionId = activeSession?.id;
    if (current.mode !== "draft" || !current.caseKey || !sessionId || !["goal", "plan", "questions", "editor"].includes(step)) {
      setDraftStep(step);
      return;
    }
    const replace = current.view === "new";
    if (step === "editor") {
      const target = draftId ?? workspace.activeDraftId ?? workspace.drafts[0]?.id;
      if (!target) {
        setDraftStep(step);
        return;
      }
      navigate(paths.draft(current.caseKey, sessionId, target), { replace });
      return;
    }
    navigate(paths.draftingSessionView(current.caseKey, sessionId, step), { replace });
  }

  // Stable, because the gym panel lists it among its callbacks' dependencies.
  const gymNavigate = React.useCallback(({ workspaceId, runId } = {}, options = {}) => {
    navigate(!workspaceId ? paths.argumentGym() : runId ? paths.gymRun(workspaceId, runId) : paths.gymWorkspace(workspaceId), options);
  }, [navigate]);

  // Switching documents is navigation too, so a reload keeps the one on screen.
  function openDraftDocument(draftId) {
    const current = parseLocation(window.location.pathname);
    if (current.mode === "draft" && current.caseKey && session?.id) {
      navigate(paths.draft(current.caseKey, session.id, draftId));
      return;
    }
    dispatchWorkspace({ type: "documentSelected", draftId });
  }

  function handleCaseAction(action) {
    if (action.type === "custom_motion") {
      setDraftMode("draft_from_scratch");
      setInstructions(action.instructions || action.summary || "");
      setDraftGoal(action.instructions || action.summary || "");
      goToMode("draft");
      return;
    }
    if (action.type === "draft_template") {
      setDraftMode("draft_from_template");
      setDraftGoal(action.instructions || action.summary || "");
      goToMode("draft");
      return;
    }
    if (action.type === "review_documents") {
      goToMode("case");
      return;
    }
    if (action.type === "search_sources") {
      goToMode("research");
      return;
    }
    if (action.type === "case_chat" && action.prompt) goToMode("case_chat");
  }

  const legalserverLoading = workspaceLoading && !legalserver;
  const legalserverConfigured = legalserver?.configured !== false;
  const legalserverConnected = Boolean(legalserver?.connected);
  const legalserverStatusLabel = legalserverLoading
    ? "Checking account"
    : !legalserverConfigured
      ? "Not configured"
      : legalserverConnected
        ? `Connected as ${legalserver.identifier}`
        : "Connect LegalServer";
  const accountName = auth?.name || auth?.email || auth?.username || "Account";
  const sources = boot?.sources || [];
  const sharepointSource = sources.find((source) => source.kind === "sharepoint");
  const researchSources = sources.filter((source) => !["legalserver", "sharepoint"].includes(source.kind));
  const sourceSummary = useMemo(() => {
    const connectionStates = [legalserverConnected, Boolean(sharepointSource?.status?.startsWith("Connected"))];
    const connectedCount = connectionStates.filter(Boolean).length;
    return `${connectedCount} of ${connectionStates.length} connections active`;
  }, [legalserverConnected, sharepointSource?.status]);

  // A case screen draws its work only once the case its URL names has loaded.
  // Until then -- or if it never will -- a notice stands in, so one client's
  // screen is never drawn under another client's address.
  const caseScoped = CASE_SCOPED_MODES.has(mode);
  const caseNotice = caseScoped && ["loading", "unavailable", "error", "not_connected", "identity_mismatch"].includes(caseState) ? caseState : null;
  const caseNoticeAction = ["not_connected", "identity_mismatch"].includes(caseNotice)
    ? { label: caseNotice === "not_connected" ? "Connect LegalServer" : "Check LegalServer connection", onClick: () => setConnectionSettingsOpen(true) }
    : null;
  // The case browser stays usable beneath its notice; that is where the
  // advocate goes next.
  const view = caseNotice && mode !== "case" ? null : mode;
  const draftScreen = view === "draft"
    ? draftScreenFor(route, {
      localStep: localDraftStep,
      sessionReady: sessionInMemory,
      draftPresent: routeDraftId == null || workspace.drafts.some((item) => item.id === routeDraftId),
    })
    : null;
  const draftingCaseKey = matter?.routeCaseKey || route.caseKey;
  // An assessment named in the URL is shown only if this case has it; the
  // latest one never stands in for it.
  const routeAssessmentId = route.mode === "triage" ? route.assessmentId : null;
  const routeAssessment = routeAssessmentId ? triageHistory.find((item) => item.id === routeAssessmentId) || null : null;
  const routeAssessmentMissing = Boolean(routeAssessmentId) && triageHistoryFor === selectedMatterId && selectedMatterId != null && !routeAssessment;
  const shownAssessment = routeAssessmentId ? routeAssessment : triageAssessment;

  if (!auth?.isAuthenticated) {
    return (
      <main className="login-screen">
        <WakingNotice />
        <form className="login-panel" onSubmit={handleLogin}>
          <h1 className="brand login-brand"><img className="clara-logo" src="/clara-1-lighthouse.svg" alt="CLARA — Cleveland Legal Aid Research Assistant" /></h1>
          <label className="field"><span>Username</span><input className="form-control" autoComplete="username" value={credentials.username} onChange={(event) => setCredentials((current) => ({ ...current, username: event.target.value }))} /></label>
          <label className="field"><span>Secret</span><input className="form-control" autoComplete={"current-" + "password"} type={"pass" + "word"} value={credentials.secret} onChange={(event) => setCredentials((current) => ({ ...current, secret: event.target.value }))} /></label>
          <button className="btn btn-light full" type="button" disabled={authBusy} onClick={handleOffice365Login}>{authBusy ? <Loader2 className="spin" size={16} /> : <Cloud size={16} />} Sign in with Office 365</button>
          {error && <div className="inline-error alert alert-danger">{error}</div>}
          <button className="btn btn-primary full" disabled={authBusy || !credentials.username || !credentials.secret}>{authBusy ? <Loader2 className="spin" size={16} /> : <LogIn size={16} />} Sign in</button>
          <a className="btn btn-light link-button full" href={api.adminUrl()}><Settings size={16} /> Admin</a>
        </form>
      </main>
    );
  }

  return (
    <div className="app-shell">
      <WakingNotice />
      <aside className="sidebar">
        <div className="brand"><img className="clara-logo" src="/clara-1-lighthouse-on-dark.svg" alt="CLARA — Cleveland Legal Aid Research Assistant" /></div>
        <nav className="mode-list">{modeOptions.map((item) => { const Icon = item.icon; return <button key={item.id} className={mode === item.id ? "active" : ""} onClick={() => goToMode(item.id)}><Icon size={18} /><span className="mode-item-label"><span>{item.label}</span>{item.id === "draft" && selectedDraftDocument && <small>Document: {selectedDraftDocument.title}</small>}</span></button>; })}</nav>
        {["goal", "plan", "questions", "editor"].includes(draftScreen) && <WorkflowStepper steps={workflowSteps} activeStep={draftStep} onSelect={showDraftStep} />}
        <div className="source-card">
          <button className="source-card-toggle" type="button" aria-expanded={sourceDetailsOpen} onClick={() => setSourceDetailsOpen((current) => !current)}><span className="source-card-title"><Archive size={16} /> Connections</span><span className="source-summary">{sourceSummary}</span><ChevronDown className={sourceDetailsOpen ? "chevron open" : "chevron"} size={16} /></button>
          {sourceDetailsOpen && <div className="source-details"><button className="source-row source-row-button" type="button" onClick={() => setConnectionSettingsOpen(true)} disabled={legalserverLoading}><span>LegalServer account</span><small>{legalserverStatusLabel}</small></button>{legalserver?.syncError && legalserver.syncError !== "not_connected" && <div className="source-row source-row-alert"><span>LegalServer sync</span><small>{legalserver.syncError}</small></div>}{sharepointSource && <div className="source-row" key={sharepointSource.kind}><span>{sharepointSource.label}</span><small>{sharepointSource.status}</small></div>}{researchSources.length > 0 && <div className="source-section-title">Research sources</div>}{researchSources.map((source) => <div className="source-row" key={source.kind}><span>{source.label}</span><small>{source.status}</small></div>)}</div>}
        </div>
      </aside>
      <main className="workspace">
        <header className="topbar">{matter && <div className="topbar-case"><div className="topbar-case-heading"><h2><button className="topbar-case-preview" type="button" aria-haspopup="dialog" title="Open case preview" onClick={() => setCasePreviewMatterId(matter.id)}><span>{matter.client}</span><FolderOpen size={17} /></button></h2>{selectedDraftDocument && <span className="selected-document-pill" title={`Selected document: ${selectedDraftDocument.title}`}><FileText size={14} /><span>{selectedDraftDocument.title}</span></span>}</div><div className="active-case-banner"><span>{matter.matter}{matter.posture ? ` · ${matter.posture}` : ""}</span><small>{matter.sourceSystem || "LegalServer"} case {matter.id}</small></div></div>}<div className="topbar-actions"><div className="dropdown account-dropdown"><button className="btn btn-light dropdown-toggle account-menu-toggle" type="button" aria-expanded={accountMenuOpen} onClick={() => setAccountMenuOpen((current) => !current)}><UserRound size={16} /><span className="account-name">{accountName}</span></button>{accountMenuOpen && <div className="dropdown-menu dropdown-menu-end show account-menu"><div className="account-menu-header"><span>Signed in as</span><strong>{accountName}</strong></div><button className="dropdown-item" type="button" onClick={() => { setAccountMenuOpen(false); setProfileOpen(true); }}><UserRound size={16} /> Profile</button><button className="dropdown-item" disabled={authBusy} type="button" onClick={handleLogout}><LogOut size={16} /> Sign out</button><div className="dropdown-divider" /><a className="dropdown-item" href={api.adminUrl()}><Settings size={16} /> Admin</a></div>}</div></div></header>
        {profileOpen && <div className="modal-backdrop" role="presentation"><div className="profile-modal" ref={profileModalRef} role="dialog" aria-modal="true" aria-label="Profile"><div className="modal-heading"><h4>Profile</h4><button className="btn btn-light icon-button" type="button" onClick={() => setProfileOpen(false)} title="Close" aria-label="Close"><X size={16} /></button></div><AuthorProfile user={auth} onSaved={(profile) => { updateAuthProfile(profile); setProfileOpen(false); }} /><ChangePassword /></div></div>}
        {connectionSettingsOpen && <div className="modal-backdrop" role="presentation"><form className="profile-modal connection-modal" ref={connectionModalRef} role="dialog" aria-modal="true" aria-label="LegalServer connection settings" onSubmit={handleLegalServerConnect}><div className="modal-heading"><div><h4>LegalServer Connection</h4><p className="modal-subtitle">{legalserverLoading ? "Checking your saved account." : legalserverConnected ? `Connected as ${legalserver.identifier}` : "Connect a LegalServer account to load assigned matters."}</p></div><button className="btn btn-light icon-button" type="button" onClick={() => setConnectionSettingsOpen(false)} title="Close" aria-label="Close"><X size={16} /></button></div>{!legalserverConfigured && <div className="inline-error">LegalServer API credentials are not configured for this environment.</div>}{legalserver?.syncError && legalserver.syncError !== "not_connected" && <div className="inline-error">LegalServer sync: {legalserver.syncError}</div>}{legalserverConfigured && <><label className="field"><span>{legalserverConnected ? "Connected as" : "LegalServer username or email"}</span><input className="form-control" aria-label="LegalServer identifier" disabled={legalserverLoading || accountBusy} value={legalserverIdentifier} onChange={(event) => setLegalserverIdentifier(event.target.value)} /></label><div className="button-row"><button className="btn btn-primary" type="submit" disabled={legalserverLoading || accountBusy || !legalserverIdentifier.trim()}>{accountBusy ? <Loader2 className="spin" size={16} /> : <Link2 size={16} />}{legalserverConnected ? "Update connection" : "Connect LegalServer"}</button>{legalserverConnected && <button className="btn btn-light" type="button" disabled={accountBusy} onClick={handleLegalServerDisconnect}>{accountBusy ? <Loader2 className="spin" size={16} /> : <Unplug size={16} />} Disconnect</button>}</div></>}</form></div>}
        {error && <div className="error-banner alert alert-danger">{error}</div>}
        {!route.found && <RouteNotice kind="not_found" onChooseCase={() => navigate(paths.cases())} />}
        {caseNotice && <RouteNotice kind={caseNotice} caseKey={route.caseKey} action={caseNoticeAction} onChooseCase={() => navigate(paths.cases())} />}
        {view === "case" && <CaseSelector cases={cases} selectedMatterId={selectedMatterId} onSelect={selectCaseById} onPreview={setCasePreviewMatterId} legalserver={legalserver} legalserverLoading={legalserverLoading} search={caseSearch} onSearchChange={setCaseSearch} onSearch={handleCaseSearch} onSearchReset={handleCaseSearchReset} filters={caseFilters} onFiltersChange={applyCaseFilters} listMeta={caseListMeta} onShowMore={() => loadCases({ append: true })} caseBusy={caseBusy} manualCaseBusy={manualCaseBusy} onCreateManualCase={handleCreateManualCase} />}
        {view === "triage" && routeAssessmentMissing && (
          <RouteNotice kind="assessment_unavailable" caseKey={route.caseKey} onChooseCase={() => navigate(paths.triage(route.caseKey))} />
        )}
        {view === "triage" && !routeAssessmentMissing && <TriagePanel matter={matter} rubrics={triageRubrics} selectedRubricId={selectedTriageRubricId} onSelectRubric={setSelectedTriageRubricId} assessment={shownAssessment} history={triageHistory} assessmentHref={(id) => paths.triageAssessment(matter?.routeCaseKey || route.caseKey, id)} onOpenAssessment={(id) => navigate(paths.triageAssessment(matter?.routeCaseKey || route.caseKey, id))} busy={busy} manualCaseBusy={manualCaseBusy} onRunTriage={runTriage} onCreateManualCase={handleCreateManualCase} legalserverSave={boot?.legalserverSave} legalserverDelivery={triageDelivery} />}
        {view === "case_chat" && (
          <CaseChat
            matter={matter}
            onAction={handleCaseAction}
            legalserverSave={boot?.legalserverSave}
            threadId={route.threadId}
            onThreadChange={(id) => {
              const caseKey = matter?.routeCaseKey || route.caseKey;
              navigate(id ? paths.chatThread(caseKey, id) : paths.chat(caseKey));
            }}
          />
        )}
        {view === "template_fill" && (
          <TemplateFillPanel
            key={matter?.id || matter?.externalId || "none"}
            matter={matter}
            authorProfile={draftAuthorProfile}
            legalserverSave={boot?.legalserverSave}
            account={auth?.username || ""}
            route={{ sessionId: route.sessionId, jobId: route.jobId, view: route.view }}
            onNavigate={({ sessionId, jobId, view: fillView } = {}, options = {}) => {
              const caseKey = matter?.routeCaseKey || route.caseKey;
              const to = !sessionId ? paths.templateFill(caseKey)
                : jobId ? paths.fillJob(caseKey, sessionId, jobId)
                  : paths.fillSession(caseKey, sessionId, fillView || "fields");
              navigate(to, options);
            }}
          />
        )}
        {view === "advice_letter" && route.view === null && (
          <SavedSessionList
            matter={matter}
            title="Advice letters"
            description="Letters saved for this case. Opening one shows it as it was saved; nothing is reassembled."
            {...savedLetters}
            sessions={savedLetters.sessions.filter((row) => row.draftId)}
            onLoadMore={savedLetters.loadMore}
            sessionHref={(row) => paths.adviceLetter(draftingCaseKey, row.draftId)}
            newHref={draftingCaseKey ? paths.adviceLetterNew(draftingCaseKey) : "/advice-letters"}
            newLabel="Start a new letter"
            onNavigate={(href) => navigate(href)}
          />
        )}
        {view === "advice_letter" && route.view === "new" && draftingCaseKey && (
          <p className="drafting-saved-link">
            <a href={paths.adviceLetters(draftingCaseKey)} onClick={(event) => { if (event.button === 0 && !event.metaKey && !event.ctrlKey) { event.preventDefault(); navigate(paths.adviceLetters(draftingCaseKey)); } }}>
              <FolderOpen size={14} /> Saved letters for this case
            </a>
          </p>
        )}
        {view === "advice_letter" && route.view !== null && (
          <AdviceLetterPanel
            // A new letter and each saved one are separate screens; switching
            // between them starts clean rather than carrying one into another.
            key={`${matter?.id || "none"}:${route.draftId || "new"}`}
            matter={matter}
            authorProfile={draftAuthorProfile}
            legalserverSave={boot?.legalserverSave}
            account={auth}
            draftId={route.draftId}
            view={route.view}
            onDraftCreated={(id) => navigate(paths.adviceLetter(draftingCaseKey, id), { replace: true })}
            onDirtyChange={setPanelDirty}
            registerSave={(save) => { panelSaveRef.current = save; }}
          />
        )}
        {view === "argument_gym" && (
          <ArgumentGymPanel
            matter={matter}
            cases={cases}
            focusRun={gymFocusRun}
            onFocusRunHandled={() => setGymFocusRun(null)}
            workspaceId={route.mode === "argument_gym" ? route.workspaceId : null}
            runId={route.mode === "argument_gym" ? route.runId : null}
            onNavigate={gymNavigate}
          />
        )}
        {view === "research" && (
          <ResearchPanel
            matter={matter}
            sources={boot?.sources || []}
            onResults={(results) => setSourceResults(results)}
            legalserverSave={boot?.legalserverSave}
            route={route}
            locationState={location.state}
            onNavigate={(to, options) => navigate(to, options)}
          />
        )}
        {draftScreen === "list" && (
          <SavedSessionList
            matter={matter}
            title="Drafting"
            description="Saved drafting work for this case. Opening one shows it as it was saved; nothing is regenerated."
            {...savedDraftingSessions}
            onLoadMore={savedDraftingSessions.loadMore}
            sessionHref={(row) => paths.draftingSession(draftingCaseKey, row.id)}
            newHref={draftingCaseKey ? paths.draftingNew(draftingCaseKey) : paths.draftingHome()}
            newLabel="Start new drafting"
            onNavigate={(href) => { if (href.endsWith("/new")) setDraftStep("goal"); navigate(href); }}
          />
        )}
        {(draftScreen === "resolving" || draftScreen === "session_pending") && (
          <RouteNotice
            kind={sessionLookup.id === routeSessionId && ["unavailable", "error"].includes(sessionLookup.status) ? `session_${sessionLookup.status}` : "session_loading"}
            caseKey={route.caseKey}
            onChooseCase={() => navigate(paths.drafting(draftingCaseKey))}
          />
        )}
        {draftScreen === "draft_missing" && (
          <RouteNotice kind="draft_unavailable" caseKey={route.caseKey} onChooseCase={() => navigate(paths.draftingSession(draftingCaseKey, routeSessionId))} />
        )}
        {draftScreen === "job" && (
          <DraftJobProgress
            progress={jobProgress.jobId === routeJobId ? jobProgress : { status: "pending", error: "" }}
            onBackToPlan={() => navigate(paths.draftingSessionView(draftingCaseKey, routeSessionId, "plan"))}
          />
        )}
        {route.view === "new" && draftScreen === "goal" && draftingCaseKey && (
          <p className="drafting-saved-link">
            <a href={paths.drafting(draftingCaseKey)} onClick={(event) => { if (event.button === 0 && !event.metaKey && !event.ctrlKey) { event.preventDefault(); navigate(paths.drafting(draftingCaseKey)); } }}>
              <FolderOpen size={14} /> Saved drafting work for this case
            </a>
          </p>
        )}
        {sessionInMemory && ["goal", "plan", "questions"].includes(draftScreen) && (
          <SaveStatusBar
            status={saveStatus({ dirty: sessionDirty, saving: sessionSave.saving, failed: sessionSave.failed, conflict: Boolean(sessionSave.conflict) })}
            error={sessionSave.error}
            onSave={saveSessionChanges}
            onKeepMine={() => resolveSessionConflict("mine")}
            onLoadTheirs={() => resolveSessionConflict("theirs")}
          />
        )}
        {draftScreen === "goal" && <DraftGoalPanel goal={draftGoal} onGoalChange={(value) => { setDraftGoal(value); setInstructions(value); setSelectedGoalSuggestionId(""); }} planningMode={planningMode} onPlanningModeChange={setPlanningMode} allowMultiple={allowMultipleDocuments} onAllowMultipleChange={setAllowMultipleDocuments} selectedTemplateId={selectedTemplateId} onTemplateChange={selectDraftTemplate} templates={templates} matter={matter} busy={busy} onMakePlan={() => makeDraftPlan()} goalSuggestions={goalSuggestions} goalSuggestionGuidance={goalSuggestionGuidance} goalSuggestionsBusy={goalSuggestionsBusy} selectedGoalSuggestionId={selectedGoalSuggestionId} onSuggestGoals={suggestDraftGoals} onSelectGoalSuggestion={selectGoalSuggestion} />}
        {draftScreen === "plan" && <DraftPlanReview plan={draftPlan} templates={templates} matter={matter} session={session} busy={busy} authorProfile={draftAuthorProfile} onAuthorProfileChange={setDraftAuthorProfile} selectedFactIds={selectedFactIds} selectedCuratedFacts={selectedCuratedFacts} onFactChange={setSelectedFactIds} onCuratedChange={setSelectedCuratedFacts} onMatterChange={setMatter} onFactIdsAdded={(ids) => setSelectedFactIds((current) => mergeFactIds(current, ids))} selectedResults={sourceResults} onSelectedResultsChange={setSourceResults} onSessionChange={setSession} candidateIssues={candidateIssues} onIssuesChange={setCandidateIssues} clarifyMissingFactsBeforeDraft={clarifyMissingFactsBeforeDraft} onClarifyMissingFactsBeforeDraftChange={setClarifyMissingFactsBeforeDraft} onPlanChange={setDraftPlan} onRegeneratePlan={regenerateDraftPlan} onContinue={goToQuestionsOrGenerate} />}
        {draftScreen === "questions" && (
          <DraftQuestionsReview
            plan={draftPlan}
            busy={busy}
            onPlanChange={setDraftPlan}
            onBack={() => showDraftStep("plan")}
            onContinue={generateDraftsFromPlan}
          />
        )}
        {draftScreen === "editor" && (
          <section className="panel editor-panel">
            <DraftSwitcher
              drafts={drafts}
              activeDraftId={draft?.id ?? null}
              onSelect={openDraftDocument}
              busy={busy}
            />
            <DraftEditor
              draft={draft}
              busy={busy}
              onChange={(sections, plainText, editorState) => dispatchWorkspace({ type: "documentPatched", patch: { sections, plainText, editorState } })}
              onPersist={() => saveDraftDocument(draft)}
              onRegenerateBlock={regenerateDraftBlock}
              onFillMissingField={fillMissingField}
            />
            <PackagePanel
              sessionId={session?.id ?? null}
              drafts={drafts}
              validatedDraftIds={workspace.validatedDraftIds}
              activeDraftId={draft?.id ?? null}
              onSelectDocument={openDraftDocument}
              key={`package-${route.view === "package"}`}
              initiallyOpen={route.view === "package"}
            />
            <DocumentHistoryPanel
              draft={draft}
              busy={busy}
              onDraftRestored={(restored) => dispatchWorkspace({ type: "documentEdited", draft: restored })}
              key={`history-${route.view === "history"}`}
              initiallyOpen={route.view === "history"}
            />
            {route.view === "validation" && draft && !(draft.validationFlags?.length > 0 || validationSummary) && (
              // Opening this URL shows what is stored; it does not run a check.
              <p className="muted validation-stored-note">No findings are stored for this document. Validate runs a fresh check.</p>
            )}
            {draft?.validation?.state === "stale" && (draft.validationFlags?.length > 0) && (
              <p className="muted validation-stored-note">These findings checked an earlier version of this document. Recheck to see whether they still apply.</p>
            )}
            {draft && (draft.validationFlags?.length > 0 || validationSummary) && (
              <ValidationPanel
                findings={draft.validationFlags || []}
                summary={validationSummary}
                onReviseWithAI={openRevisionPlan}
                reviseBusy={revisionBusy}
              />
            )}
            {draft && (
              <LegalServerSaveToggle
                kind="documents"
                checked={saveDraftToLegalServer}
                onChange={setSaveDraftToLegalServer}
                bootstrapSave={boot?.legalserverSave}
                delivery={draftDelivery}
                disabled={exportBusy}
              />
            )}
            {draft && sessionInMemory && (
              <SaveStatusBar
                thing="document"
                status={saveStatus({
                  dirty: workspace.unsavedDraftIds.includes(draft.id),
                  saving: documentSave.saving,
                  failed: documentSave.failed,
                  conflict: workspace.conflict?.draftId === draft.id,
                })}
                error={documentSave.error}
                // The editor has its own Save; the bar offers one only to retry.
                onSave={documentSave.failed ? () => saveDraftDocument(draft) : undefined}
                onKeepMine={() => resolveDocumentConflict("mine")}
                onLoadTheirs={() => resolveDocumentConflict("theirs")}
              />
            )}
            <div className="button-row step-actions bottom-step-actions">
              <button className="btn btn-light" onClick={() => showDraftStep("plan")}>Back to plan</button>
              {draft && (
                <div className="button-row compact bottom-step-actions-right">
                  {draftDirtySinceValidation && validationSummary && (
                    <span className="recheck-hint">You've made changes since the last check.</span>
                  )}
                  <button
                    className="btn btn-light"
                    type="button"
                    disabled={stressTestBusy}
                    onClick={stressTestDraft}
                    title="Have an opponent, a judge, and a coach read this draft"
                  >
                    {stressTestBusy ? <Loader2 className="spin" size={16} /> : <Swords size={16} />} Stress test
                  </button>
                  <button
                    className="btn btn-light"
                    type="button"
                    disabled={exportBusy}
                    onClick={() => exportDraftDocument(draft)}
                  >
                    {exportBusy ? <Loader2 className="spin" size={16} /> : <Download size={16} />} {draft.exportFormat === "xlsx" ? "Export workbook" : "Export to Word"}
                  </button>
                  <button
                    className={`btn btn-primary${draftDirtySinceValidation ? " suggested-next-step" : ""}`}
                    disabled={busy}
                    type="button"
                    onClick={validateDraft}
                  >
                    {busy ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />} {validationSummary ? "Recheck" : "Validate"}
                  </button>
                </div>
              )}
            </div>
            <RevisionPlanModal
              plan={revisionPlan}
              busy={revisionBusy}
              onClose={() => dispatchWorkspace({ type: "revisionPlanLoaded", plan: null })}
              onUpdateItem={updateRevisionPlanItem}
              onApply={applyRevisionPlan}
            />
          </section>
        )}
        <UnsavedChangesDialog
          open={blocker.state === "blocked"}
          busy={leaveSave.busy}
          error={leaveSave.error}
          onSave={saveAndLeave}
          onDiscard={discardAndLeave}
          onStay={() => { setLeaveSave({ busy: false, error: "" }); blocker.reset?.(); }}
        />
        <CasePreviewModal
          matter={casePreviewMatter}
          isActive={Boolean(casePreviewMatter && casePreviewMatter.id === selectedMatterId)}
          manualCaseBusy={manualCaseBusy}
          onClose={() => setCasePreviewMatterId(null)}
          onOpen={openCase}
          onUpdateManualCase={handleUpdateManualCase}
        />
      </main>
    </div>
  );
}

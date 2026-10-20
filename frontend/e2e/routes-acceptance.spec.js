import { expect, test } from "@playwright/test";

// Acceptance journeys for readable, reload-safe routes (issue #73), against a
// stubbed API that can change its answers mid-test: who is signed in, and
// whether the server has woken up yet. Like screens-render.spec.js it needs no
// Django, no database, and no LegalServer.

const CASE_A = {
  id: "MID-1", routeCaseKey: "26-0001", caseNumber: "26-0001", client: "Sample Client",
  matter: "Summary process", posture: "Answer due", sourceSystem: "Sample", facts: [], documents: [], notes: [],
};
const SESSION = {
  id: 184, mode: "draft_from_template", status: "draft_review", matter: CASE_A, template: null,
  selectedFactIds: [], selectedCuratedFacts: [], selectedSourceResults: [], selectedBlockKeys: [],
  authorProfile: {}, templateData: {}, goal: "Answer the complaint", instructions: "Answer the complaint",
  draftPlan: { documents: [{ title: "Answer to complaint" }] }, missingInformation: [], selectedTemplateIds: [],
  workflowOptions: {}, revision: 2, updatedAt: "2026-09-25T12:00:00Z",
};

function user(username) {
  return { isAuthenticated: true, name: username, username, profile: {} };
}

// server.signedIn: null or a username; server.canOpen(username) -> bool;
// server.wakeFailures: 500s left before case lookups answer.
async function stubServer(page, server) {
  const writes = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, "");
    if (request.method() !== "GET") writes.push(`${request.method()} ${path}`);
    const json = (body, status = 200) => route.fulfill({ status, json: body });

    if (path === "/auth/me/") return json({ user: server.signedIn ? user(server.signedIn) : { isAuthenticated: false } });
    if (path === "/auth/login/") {
      server.signedIn = request.postDataJSON().username;
      return json({ user: user(server.signedIn) });
    }
    if (path === "/auth/logout/") {
      server.signedIn = null;
      return json({ ok: true });
    }
    if (!server.signedIn) return json({ error: "Authentication required" }, 401);
    if (path === "/cases/by-route-key/") {
      if (server.wakeFailures > 0) {
        server.wakeFailures -= 1;
        return route.fulfill({ status: 500, contentType: "text/html", body: "<h1>Server Error</h1>" });
      }
      const key = url.searchParams.get("key");
      if ((key === "26-0001" || key === "MID-1") && server.canOpen(server.signedIn)) return json({ case: CASE_A });
      return json({ error: "Case not found or not available to this user" }, 404);
    }
    if (path === "/cases/") return json({ cases: server.canOpen(server.signedIn) ? [CASE_A] : [], legalserver: null, total: 1 });
    if (path === "/drafting-sessions/184/") return json({ session: SESSION, resume: { recommendedView: "plan", draftIds: [], lastDraftId: null } });
    if (path === "/drafting-sessions/184/drafts/") return json({ drafts: [] });
    if (path === "/bootstrap/") return json({ sources: [], legalserverSave: {}, modes: [] });
    if (path === "/templates/") return json({ templates: [] });
    if (path === "/triage/rubrics/") return json({ rubrics: [] });
    return json({});
  });
  return writes;
}

async function signIn(page, username) {
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Secret").fill("not-a-real-secret");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

test("password sign-in waits for the initial session and CSRF handshake", async ({ page }) => {
  const writes = await stubServer(page, { signedIn: null, canOpen: () => true, wakeFailures: 0 });
  let releaseHandshake;
  const handshake = new Promise((resolve) => { releaseHandshake = resolve; });
  await page.route("**/api/auth/me/", async (route) => {
    await handshake;
    await route.fulfill({ json: { user: { isAuthenticated: false } } });
  });
  await page.goto("/");
  await page.getByLabel("Username").fill("ada");
  await page.getByLabel("Secret").fill("not-a-real-secret");
  const submit = page.getByRole("button", { name: "Sign in", exact: true });
  try {
    await expect(submit).toBeDisabled();
    await page.getByLabel("Secret").press("Enter");
    expect(writes).not.toContain("POST /auth/login/");
  } finally {
    releaseHandshake();
  }
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByRole("heading", { name: "Cases" })).toBeVisible();
});

test("a deep link survives signing in with a password", async ({ page }) => {
  await stubServer(page, { signedIn: null, canOpen: () => true, wakeFailures: 0 });
  await page.goto("/drafting/26-0001/sessions/184/plan");
  await signIn(page, "ada");
  await expect(page).toHaveURL(/\/drafting\/26-0001\/sessions\/184\/plan$/);
  await expect(page.locator(".topbar-case")).toContainText(CASE_A.client);
  await expect(page.locator(".stepper")).toBeVisible();
});

test("another account signing in on the same tab never sees the last account's case", async ({ page }) => {
  await stubServer(page, { signedIn: "ada", canOpen: (name) => name === "ada", wakeFailures: 0 });
  await page.goto("/cases/26-0001");
  await expect(page.locator(".topbar-case")).toContainText(CASE_A.client);

  await page.getByRole("button", { name: "ada" }).click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "bob");
  await expect(page.getByRole("heading", { name: "Case 26-0001 is not available" })).toBeVisible();
  await expect(page.getByText(CASE_A.client)).toHaveCount(0);
});

test("a link opened while the server is waking up opens once it answers", async ({ page }) => {
  await stubServer(page, { signedIn: "ada", canOpen: () => true, wakeFailures: 2 });
  await page.goto("/triage/26-0001");
  await expect(page.locator(".topbar-case")).toContainText(CASE_A.client, { timeout: 20_000 });
  await expect(page.getByText("is not available")).toHaveCount(0);
});

test("Back and Forward through saved work never send anything", async ({ page }) => {
  const writes = await stubServer(page, { signedIn: "ada", canOpen: () => true, wakeFailures: 0 });
  await page.goto("/cases/26-0001");
  await expect(page.locator(".topbar-case")).toContainText(CASE_A.client);
  await page.goto("/drafting/26-0001");
  await page.goto("/drafting/26-0001/sessions/184");
  await expect(page).toHaveURL(/\/sessions\/184\/plan$/);
  await page.goto("/triage/26-0001");
  for (let pass = 0; pass < 2; pass += 1) {
    await page.goBack();
    await page.goBack();
    await page.goForward();
    await page.goForward();
  }
  await expect(page).toHaveURL(/\/triage\/26-0001$/);
  expect(writes).toEqual([]);
});

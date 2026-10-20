import { expect, test } from "@playwright/test";

import { docxText } from "./support/docx.js";
import { apiCall, guardNetwork, modelTimeout, openScreen, selectCase, signIn } from "./support/deployment.js";

// The drafting workflow, end to end: goal, plan, the template's open blanks,
// generation, the editor, validation, AI revision, history, and export. One
// journey rather than a test per step, because each step needs the last one's
// document and generation is the slow, model-backed part.

const HEAT_CASE = { caseNumber: "26-0000085", client: "Christopher Anderson" };
const HEAT_TEMPLATE = "Defendant's Emergency Motion for Heat (Cleveland Housing Court)";

const isDraftsPost = (response) =>
  /\/drafting-sessions\/\d+\/drafts\/$/.test(new URL(response.url()).pathname) && response.request().method() === "POST";

async function planKnownTemplate(page, { template, goal }) {
  await openScreen(page, "Draft");
  await page.getByRole("button", { name: /I already know the template/ }).click();
  await page.getByLabel("Template").selectOption({ label: template });
  await page.getByLabel("Goal or extra instructions").fill(goal);
  await page.getByRole("button", { name: "Make plan" }).click();
  await expect(page.getByRole("heading", { name: goal })).toBeVisible({ timeout: modelTimeout });
}

function blockEditor(page, heading) {
  return page
    .getByRole("heading", { name: heading, level: 4 })
    .locator("xpath=ancestor::*[.//*[@role='textbox']][1]")
    .getByRole("textbox");
}

test.describe("drafting from a known template", () => {
  test("plan, answer the blanks, generate, edit, validate, and export", async ({ page }) => {
    test.setTimeout(modelTimeout * 6);
    const guard = guardNetwork(page);
    await signIn(page);
    await selectCase(page, HEAT_CASE);
    const marker = `E2E human edit ${Date.now()}`;
    let draftId;

    await test.step("plan", async () => {
      await planKnownTemplate(page, {
        template: HEAT_TEMPLATE,
        goal: "Seek emergency relief to restore heat based on the gas-company inspection.",
      });
      const selectedTemplate = page.getByLabel("Recommended template");
      await expect(selectedTemplate.locator("option:checked")).toHaveText(HEAT_TEMPLATE);
      // Every section the template requires is planned, and cannot be dropped.
      await expect(page.getByRole("checkbox", { name: "Relevant Facts" })).toBeChecked();
    });

    await test.step("the template's open blanks", async () => {
      await page.getByRole("button", { name: "Review questions" }).click();
      await expect(page.getByRole("heading", { name: "Fill the blanks the case record could not" })).toBeVisible();
      const continueButton = page.getByRole("button", { name: "Continue to draft" });
      // Nothing proceeds until every blank is answered or deliberately skipped.
      await expect(continueButton).toBeDisabled();
      const first = page.getByRole("textbox", { name: "Answer" }).first();
      await first.fill("Unit 2, 1200 Example Avenue, Cleveland, OH 44102");
      for (let guardCount = 0; guardCount < 40; guardCount += 1) {
        const skip = page.getByRole("button", { name: /^Skip/ });
        if (!(await skip.count())) break;
        await skip.first().click();
      }
      await expect(continueButton).toBeEnabled();
      const generated = page.waitForResponse(isDraftsPost, { timeout: modelTimeout });
      await continueButton.click();
      const response = await generated;
      // Generation runs in the background: 202 and a job to poll, never a
      // request held open for the whole model run.
      expect(response.status(), "draft generation starts as a job").toBe(202);
      const { job } = await response.json();
      await expect(page.locator(".draft-editor")).toBeVisible({ timeout: modelTimeout });
      const polled = await apiCall(page, "GET", `/drafting-sessions/${job.sessionId}/drafts/?job=${job.id}`);
      expect(polled.data.job.status).toBe("complete");
      draftId = polled.data.job.draftIds[0];
    });

    await test.step("every planned section is in the draft", async () => {
      for (const heading of ["Introduction", "Relevant Facts", "Law And Argument", "Certificate Of Service"]) {
        await expect(page.getByRole("heading", { name: heading, level: 4 })).toBeVisible();
      }
      if (!draftId) {
        const session = await apiCall(page, "GET", `/drafting-sessions/${await currentSessionId(page)}/drafts/`);
        draftId = session.data.drafts?.at(-1)?.id;
      }
      expect(draftId, "the generated draft's id").toBeTruthy();
    });

    await test.step("a human edit is saved and survives a reload of the draft", async () => {
      const editor = blockEditor(page, "Introduction");
      await editor.click();
      await page.keyboard.press("Control+End");
      await page.keyboard.type(` ${marker}.`);
      const saved = page.waitForResponse(
        (response) => new URL(response.url()).pathname.endsWith(`/drafts/${draftId}/`) && response.request().method() === "PATCH",
      );
      await page.getByRole("button", { name: "Save", exact: true }).click();
      expect((await saved).ok()).toBeTruthy();
      const stored = await apiCall(page, "GET", `/drafts/${draftId}/`);
      expect(JSON.stringify(stored.data)).toContain(marker);
    });

    await test.step("validation runs and reports", async () => {
      const validated = page.waitForResponse((response) => /\/drafts\/\d+\/validate\/$/.test(new URL(response.url()).pathname), {
        timeout: modelTimeout,
      });
      await page.getByRole("button", { name: "Validate", exact: true }).click();
      expect((await validated).ok()).toBeTruthy();
      await expect(page.getByRole("button", { name: "Recheck", exact: true })).toBeVisible({ timeout: modelTimeout });
    });

    await test.step("version history lists the human edit", async () => {
      await page.getByRole("button", { name: /Version history and sources/ }).click();
      // The human edit is a second version of that section; the change log
      // lists proposed operations, which a direct edit is not.
      await expect(page.getByRole("button", { name: /^Introduction 2 versions$/ })).toBeVisible();
      const components = await apiCall(page, "GET", `/drafts/${draftId}/components/`);
      expect(components.status).toBe(200);
    });

    await test.step("export carries the human edit into the Word file", async () => {
      await page.locator(".legalserver-save input[type=checkbox]").uncheck();
      const exportResponse = page.waitForResponse((response) => /\/drafts\/\d+\/export\//.test(new URL(response.url()).pathname));
      const download = page.waitForEvent("download");
      await page.getByRole("button", { name: "Export to Word" }).click();
      const response = await exportResponse;
      expect(response.status()).toBe(200);
      expect(new URL(response.url()).searchParams.get("saveToLegalServer")).toBe("0");
      const file = await download;
      expect(file.suggestedFilename()).toMatch(/\.docx$/);
      const text = docxText(await file.path());
      expect(text, "an advocate's edit must reach the export").toContain(marker);
      expect(text).toMatch(/Emergency Motion/i);
      // This demo journey opts out explicitly, so the server must not create a
      // LegalServer delivery record or make a remote write.
      expect(response.headers()["x-legalserver-delivery"]).toBeUndefined();
    });

    guard.assertClean();
  });

  test("an AI refinement rewrites one section and leaves the rest", async ({ page }) => {
    test.setTimeout(modelTimeout * 5);
    const guard = guardNetwork(page);
    await signIn(page);
    await selectCase(page, HEAT_CASE);
    await planKnownTemplate(page, {
      template: HEAT_TEMPLATE,
      goal: "Emergency motion to restore heat; keep the facts short.",
    });
    await page.getByLabel("Pause to review the template's blanks before generating").uncheck();
    const generated = page.waitForResponse(isDraftsPost, { timeout: modelTimeout });
    await page.getByRole("button", { name: "Generate draft" }).click();
    expect((await generated).status()).toBe(202);
    await expect(page.locator(".draft-editor")).toBeVisible({ timeout: modelTimeout });

    const lawBefore = await blockEditor(page, "Law And Argument").textContent();
    const facts = page.getByRole("heading", { name: "Relevant Facts", level: 4 }).locator("xpath=ancestor::*[.//button[@aria-label='Section actions']][1]");
    await facts.getByRole("button", { name: "Section actions" }).click();
    await page.getByRole("button", { name: "Refine with AI" }).click();
    const dialog = page.getByRole("dialog", { name: "Refine section" });
    await dialog.locator("textarea").fill("Make this two sentences and mention the gas-company inspection.");
    const refined = page.waitForResponse((response) => /\/blocks\/[^/]+\/regenerate\/$/.test(new URL(response.url()).pathname), {
      timeout: modelTimeout,
    });
    await dialog.getByRole("button", { name: "Refine" }).click();
    expect((await refined).ok()).toBeTruthy();
    await expect(blockEditor(page, "Law And Argument")).toHaveText(lawBefore);
    guard.assertClean();
  });
});

test.describe("planning with AI help", () => {
  test("goals are suggested from the case facts", async ({ page }) => {
    test.setTimeout(modelTimeout * 2);
    const guard = guardNetwork(page);
    await signIn(page);
    await selectCase(page, HEAT_CASE);
    await openScreen(page, "Draft");
    const suggest = page.getByRole("button", { name: "Suggest goals from case facts" });
    test.skip(await suggest.isDisabled(), "goal suggestions need drafting facts on the case");
    const answered = page.waitForResponse((response) => /\/recommend-goals\/$/.test(new URL(response.url()).pathname), {
      timeout: modelTimeout,
    });
    await suggest.click();
    expect((await answered).ok()).toBeTruthy();
    guard.assertClean();
  });

  test("the AI recommends a template for a goal", async ({ page }) => {
    test.setTimeout(modelTimeout * 2);
    const guard = guardNetwork(page);
    await signIn(page);
    await selectCase(page, HEAT_CASE);
    await openScreen(page, "Draft");
    await page.getByRole("button", { name: /Let AI suggest template/ }).click();
    const goal = "The landlord shut off the heat in January; get it restored now.";
    await page.getByRole("textbox", { name: "Goal", exact: true }).fill(goal);
    await page.getByRole("button", { name: "Make plan" }).click();
    await expect(page.getByRole("heading", { name: goal })).toBeVisible({ timeout: modelTimeout });
    await expect(page.getByLabel("Recommended template").locator("option:checked")).toHaveText(/Heat/);
    guard.assertClean();
  });
});

test.describe("sources of fact", () => {
  test("no recommended fact is sourced from this tool's own output", async ({ page }) => {
    test.setTimeout(modelTimeout * 2);
    await signIn(page);
    await selectCase(page, HEAT_CASE);
    const materials = await apiCall(page, "GET", `/cases/${HEAT_CASE.caseNumber}/materials/`);
    const ownOutput = [
      ...materials.data.documents.filter((item) => /Emergency Motion|advice letter/i.test(item.title)),
      ...materials.data.notes.filter((item) => /^(Case chat|AI usage audit)/.test(item.title)),
    ];
    // Only meaningful where the case file holds work product this tool saved
    // there, which on the sample case it does (F17).
    test.skip(ownOutput.length === 0, "the case file holds none of this tool's output to exclude");

    const session = await apiCall(page, "POST", "/drafting-sessions/", {
      matterId: HEAT_CASE.caseNumber,
      mode: "draft_from_template",
      instructions: "Emergency motion to restore heat.",
    });
    expect(session.ok, JSON.stringify(session.data).slice(0, 300)).toBeTruthy();
    const sessionId = session.data.session?.id ?? session.data.id;
    const recommended = await apiCall(page, "POST", `/drafting-sessions/${sessionId}/recommend-facts/`, { apply: false });
    expect(recommended.ok).toBeTruthy();
    const sources = JSON.stringify(recommended.data);
    expect(sources).not.toMatch(/Case document: CLE Emergency Motion|Case chat:|AI usage audit|Client advice letter/);
  });
});

async function currentSessionId(page) {
  // The session on screen is in the address bar; the newest session is only
  // a guess when the URL names none.
  const inUrl = new URL(page.url()).pathname.match(/\/sessions\/(\d+)/);
  if (inUrl) return Number(inUrl[1]);
  const sessions = await apiCall(page, "GET", "/drafting-sessions/");
  return sessions.data.sessions?.[0]?.id;
}

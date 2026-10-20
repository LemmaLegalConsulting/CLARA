import { expect, test } from "@playwright/test";

import { apiCall, credentials, guardNetwork, requireCredentials, signIn } from "./support/deployment.js";

// The deployment itself: static hosting, sign-in, the session and CSRF cookies
// across the app/API split, and signing out. Everything else depends on these,
// and each has broken in production without any unit test noticing.

const apiBase = process.env.E2E_API_BASE || "/api";

test.describe("static hosting", () => {
  test("a deep link loads the app instead of a 404", async ({ page }) => {
    const guard = guardNetwork(page);
    const response = await page.goto("/draft/anything");
    expect(response.status()).toBe(200);
    await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
    guard.assertClean();
  });

  test("a missing asset is a 404, not the app shell", async ({ request, baseURL }) => {
    // A missing script answered with index.html fails as a MIME error in the
    // browser, far from the cause. Static Web Apps excludes /assets/* from the
    // fallback for exactly this reason.
    const response = await request.get(new URL("/assets/does-not-exist.js", baseURL).href);
    expect(response.status()).toBe(404);
  });
});

test.describe("signing in and out", () => {
  test("a wrong password is refused with a message", async ({ page }) => {
    requireCredentials();
    const guard = guardNetwork(page);
    await page.goto("/");
    await page.getByLabel("Username").fill(credentials.username);
    await page.getByLabel("Secret").fill("definitely-not-the-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.locator(".alert-danger")).toHaveText("Invalid username or password");
    await expect(page.getByRole("heading", { name: "Cases" })).toHaveCount(0);
    guard.assertClean();
  });

  test("the session and CSRF cookie work across the app and API hosts", async ({ page }) => {
    const guard = guardNetwork(page);
    await signIn(page);

    // The app reads the CSRF cookie from script to send it back as a header. On
    // the split deployment that only works when the API scoped the cookie to the
    // parent domain; otherwise every GET works and every save fails with 403.
    const csrf = await page.evaluate(() => document.cookie.includes("csrftoken="));
    expect(csrf, "the CSRF cookie must be readable by the app's own origin").toBeTruthy();

    const me = await apiCall(page, "GET", "/auth/me/");
    expect(me.status).toBe(200);
    expect(me.data.user.isAuthenticated).toBeTruthy();

    // An unsafe request is the one CSRF protects; it must pass.
    const profile = await apiCall(page, "GET", "/author-profile/");
    const saved = await apiCall(page, "PATCH", "/author-profile/", profile.data.profile || {});
    expect(saved.status, JSON.stringify(saved.data)).toBe(200);

    // The same request without the header must not.
    const forged = await page.evaluate(async (url) => {
      const response = await fetch(url, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      return response.status;
    }, `${apiBase}/author-profile/`);
    expect(forged, "an unsafe request without the CSRF header must be refused").toBe(403);
    guard.problems.splice(0); // the 403 above is the point of the check
  });

  test("the session survives a reload and a deep link", async ({ page }) => {
    const guard = guardNetwork(page);
    await signIn(page);
    await page.reload();
    await expect(page.getByRole("heading", { name: "Cases" })).toBeVisible();
    await page.goto("/research");
    await expect(page.locator("nav.mode-list")).toBeVisible();
    guard.assertClean();
  });

  test("the profile saves and comes back", async ({ page }) => {
    const guard = guardNetwork(page);
    await signIn(page);
    const before = await apiCall(page, "GET", "/author-profile/");
    const marker = `E2E Office ${Date.now()}`;

    await page.locator(".account-menu-toggle").click();
    await page.getByRole("button", { name: "Profile" }).click();
    const dialog = page.getByRole("dialog", { name: "Profile" });
    await expect(dialog).toBeVisible();
    const office = dialog.locator("label", { hasText: "Office" }).locator("input, textarea");
    await office.fill(marker);
    await dialog.getByRole("button", { name: "Save profile" }).click();
    await expect(dialog).toBeHidden();

    const after = await apiCall(page, "GET", "/author-profile/");
    expect(JSON.stringify(after.data)).toContain(marker);

    // Leave the shared test user as it was.
    await apiCall(page, "PATCH", "/author-profile/", before.data.profile || {});
    guard.assertClean();
  });

  test("the admin link points at the API host", async ({ page }) => {
    await signIn(page);
    await page.locator(".account-menu-toggle").click();
    const href = await page.locator(".account-menu a", { hasText: "Admin" }).getAttribute("href");
    const expected = new URL("/admin/", new URL(apiBase, page.url())).href;
    expect(href).toBe(expected);
    const admin = await page.request.get(href);
    expect(admin.status()).toBe(200);
  });

  test("signing out ends the session on the server", async ({ page }) => {
    const guard = guardNetwork(page);
    await signIn(page);
    await page.locator(".account-menu-toggle").click();
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
    const me = await apiCall(page, "GET", "/cases/");
    expect([401, 403]).toContain(me.status);
    guard.problems.splice(0);
  });
});

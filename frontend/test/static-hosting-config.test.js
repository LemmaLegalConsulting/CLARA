import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const configUrl = new URL("../staticwebapp.config.json", import.meta.url);

test("Static Web Apps keeps missing assets out of the SPA fallback", async () => {
  const config = JSON.parse(await readFile(configUrl, "utf8"));

  assert.equal(config.navigationFallback?.rewrite, "/index.html");
  assert.ok(
    config.navigationFallback?.exclude?.includes("/assets/*"),
    "asset requests must be excluded so a missing script or stylesheet stays a 404",
  );
  assert.equal(
    config.responseOverrides?.["404"],
    undefined,
    "a global 404 rewrite would turn missing assets back into the app shell",
  );
});

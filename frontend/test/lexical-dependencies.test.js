import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Mixed Lexical releases build successfully but crash when an editor renders.
// Catch separate Dependabot bumps before the browser smoke test reaches them.
test("Lexical and its React bindings resolve to one shared release", () => {
  const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url)));
  const lock = JSON.parse(readFileSync(new URL("../package-lock.json", import.meta.url)));
  assert.equal(manifest.dependencies.lexical, manifest.dependencies["@lexical/react"]);
  const version = lock.packages["node_modules/lexical"].version;
  for (const [path, entry] of Object.entries(lock.packages)) {
    if (/(?:^|\/)node_modules\/(?:lexical|@lexical\/[^/]+)$/.test(path)) {
      assert.equal(entry.version, version, `${path} must match lexical ${version}`);
    }
  }
});

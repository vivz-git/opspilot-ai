#!/usr/bin/env node
/**
 * Regenerate src/lib/api/schema.d.ts from the backend's live OpenAPI
 * contract. Never hand-edit that file — this script is the only writer.
 *
 * Two steps, both offline and DB-free:
 *   1. `backend/scripts/export_openapi.py` calls `app.openapi()` directly
 *      (no server, no lifespan, no Postgres needed) and writes the spec.
 *   2. `openapi-typescript` turns that spec into TypeScript types.
 */
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

const here = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(here, "..");
const backendRoot = path.resolve(frontendRoot, "../backend");
const specPath = path.join(frontendRoot, ".openapi.generated.json");
const outPath = path.join("src", "lib", "api", "schema.d.ts");

console.log("[generate:api] exporting the OpenAPI schema from app.openapi()...");
execFileSync("uv", ["run", "python", "scripts/export_openapi.py", specPath], {
  cwd: backendRoot,
  stdio: "inherit",
});

console.log(`[generate:api] generating ${outPath} with openapi-typescript...`);
execFileSync("npx", ["openapi-typescript", specPath, "-o", outPath], {
  cwd: frontendRoot,
  stdio: "inherit",
});

fs.rmSync(specPath, { force: true });
console.log(`[generate:api] wrote ${outPath}`);

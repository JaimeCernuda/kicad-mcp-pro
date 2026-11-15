// Bundles dev/check_parse.ts with the views' own aliases and runs it over the fixtures.
import esbuild from "esbuild";
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const ECAD = resolve(ROOT, "vendor/ecad-viewer");
const APP = resolve(ECAD, "packages/ecad-viewer-app");
const out = resolve(ROOT, "dist/check_parse.mjs");
await esbuild.build({
    entryPoints: [resolve(HERE, "check_parse.ts")],
    outfile: out,
    bundle: true,
    format: "esm",
    platform: "node",
    loader: { ".js": "ts", ".glsl": "text", ".css": "text", ".svg": "text", ".kicad_wks": "text" },
    alias: {
        "ecad-viewer-app": APP,
        "kicad-parser": resolve(ECAD, "packages/kicad-parser/src"),
        "@huaqiu/ecad-renderer": resolve(ECAD, "packages/ecad-renderer/src/index.ts"),
        three: resolve(APP, "static/three/three.module.js"),
        "glyph-full": resolve(ROOT, "src/glyph-stub.ts"),
    },
    nodePaths: [resolve(ROOT, "node_modules")],
    define: { DEBUG: "false" },
    logLevel: "warning",
    tsconfigRaw: { compilerOptions: { experimentalDecorators: true, useDefineForClassFields: false } },
});
try {
    execFileSync(process.execPath, [out, resolve(ROOT, "../kicad-fixtures/fixtures"), resolve(HERE, "samples")], { stdio: "inherit" });
} catch {
    process.exit(1);
}

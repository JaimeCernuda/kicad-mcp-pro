// Builds dist/kicad-views.html: one self-contained page (no network loads) holding the
// six views, ecad-viewer and its parser worker. The server serves it once per view.
//
//   node build.mjs              build from vendor/ecad-viewer
//   ECAD_ROOT=<checkout> node build.mjs --inputs   list the ecad-viewer files a build uses

import esbuild from "esbuild";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(process.env.ECAD_ROOT ?? resolve(HERE, "vendor/ecad-viewer"));
const APP = resolve(ROOT, "packages/ecad-viewer-app");

const loader = { ".js": "ts", ".glsl": "text", ".css": "text", ".svg": "text", ".kicad_wks": "text" };
const alias = {
    "ecad-viewer-app": APP,
    "kicad-parser": resolve(ROOT, "packages/kicad-parser/src"),
    "@huaqiu/ecad-renderer": resolve(ROOT, "packages/ecad-renderer/src/index.ts"),
    "3d-viewer": resolve(APP, "src/3d-viewer/index.ts"),
    three: resolve(APP, "static/three/three.module.js"),
    "glyph-full": resolve(HERE, "src/glyph-stub.ts"),
};

// three/addons/<path> → static/three/addons/<path>
const threeAddons = {
    name: "three-addons",
    setup(build) {
        build.onResolve({ filter: /^three\/addons\// }, (args) => ({
            path: resolve(APP, "static/three/addons", args.path.slice("three/addons/".length)),
        }));
    },
};

const common = {
    bundle: true,
    format: "esm",
    target: "es2022",
    keepNames: true,
    minify: process.env.KICAD_VIEWS_DEBUG ? false : true,
    legalComments: "none",
    loader,
    alias,
    plugins: [threeAddons],
    nodePaths: [resolve(HERE, "node_modules")],
    define: { DEBUG: "false" },
    // ecad-viewer uses legacy decorators (@attribute) and no class-field defines.
    tsconfigRaw: { compilerOptions: { experimentalDecorators: true, useDefineForClassFields: false } },
    logLevel: "warning",
    metafile: true,
    write: false,
};

const worker = await esbuild.build({
    ...common,
    entryPoints: [resolve(APP, "src/kicanvas/parser.worker.ts")],
});
const workerSource = worker.outputFiles[0].text;

const main = await esbuild.build({
    ...common,
    entryPoints: [resolve(HERE, "src/main.ts")],
    define: { ...common.define, __PARSER_WORKER_SOURCE__: JSON.stringify(workerSource) },
});
const script = main.outputFiles[0].text;

if (process.argv.includes("--inputs")) {
    const inputs = new Set([...Object.keys(worker.metafile.inputs), ...Object.keys(main.metafile.inputs)]);
    for (const input of [...inputs].sort()) {
        const absolute = resolve(process.cwd(), input);
        if (absolute.startsWith(ROOT)) console.log(relative(ROOT, absolute));
    }
    process.exit(0);
}

const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KiCad view</title>
</head>
<body>
<div id="kicad-views"></div>
<script type="module">${script.replaceAll("</script", "<\\/script")}</script>
</body>
</html>
`;
const out = resolve(HERE, "dist/kicad-views.html");
await mkdir(dirname(out), { recursive: true });
await writeFile(out, html);
const target = process.env.KICAD_VIEWS_OUT;
if (target) await writeFile(resolve(target), html);
console.log(`kicad-views.html: ${(html.length / 1024).toFixed(0)} KiB`);
void readFile;

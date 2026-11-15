# Vendored ecad-viewer

- Source: https://github.com/Huaqiu-Electronics/ecad-viewer (MIT, see `LICENSE.md`).
- Commit: `6f9fbf19504d1bd0f07c1f91f4b6a8d69cd07467` (6 Nov 2026).
- Only the files a build of `packages/kicad-views` uses are kept, listed by
  `ECAD_ROOT=<checkout> node build.mjs --inputs`, plus the two `tsconfig.json` files.
  `src/glyph` (the full CJK glyph set) is replaced by `src/glyph-stub.ts`.

## Changes made here

Each is marked `kicad-views` in the code and offered upstream.

| File | Change | Why |
| --- | --- | --- |
| `src/kicanvas/worker_pool.ts` | The parser worker is created from an inlined blob (`__PARSER_WORKER_SOURCE__`), not `new URL("./parser.worker.js", import.meta.url)` | The views are one self-contained HTML file; the page loads nothing |
| `src/graphics/webgl/renderer.ts` | `preserveDrawingBuffer: true` | A capture (`capture`, `describe_region`) reads the frame on screen; without it the buffer can be blank after compositing |
| `src/ecad-viewer/ecad_viewer.ts` | Public `board_viewer` and `schematic_viewer` getters | The views drive the viewers: focus, selection, capture, drag |
| `src/ecad-viewer/ecad_viewer.ts` | The vendor logo link is removed | A view opens links only through its host (`ui/open-link`) |
| `src/3d-viewer/environments.ts` | Only the environments that need no download | The two HDR maps came from storage.googleapis.com; an empty CSP blocks them |
| `src/kicad/schematic.ts` | A symbol whose library symbol is missing (no `lib_symbols` section, or no entry) draws a placeholder box | Parser fix: the view failed to load such a schematic (`Cannot read properties of undefined (reading 'by_name')`); found on the fork's `clean-led-kicad10` fixture |
| `src/3d-viewer/online_3d_viewer.ts` | The model-ready listener is removed when the viewer leaves the page; a model that fails to load raises `Online3dViewerFailed` (`3d:viewer:failed`) and can be tried again; the model URL is not revoked here | The board view replaces the element on every edit, so old viewers kept loading models; a failed load left the 3D tab spinning with no message; the view reuses its URL |
| `src/viewers/base/events.ts` | `Online3dViewerFailed` | The event above |

## Known

- `src/3d-viewer/viewer.ts` sets Draco and KTX2 decoder paths on unpkg.com. They are only
  fetched for GLB files compressed with those extensions; `kicad-cli pcb export glb` does
  not produce them, and an empty CSP would block the fetch.
- A page whose container is `<div id="app">` breaks ecad-viewer: it reads `window.app` as
  its mode, and browsers expose element ids on `window`. The views use `id="kicad-views"`.
- `packages/kicad-parser/src/board_parser.ts`: KiCad 11 (file version 20261025) writes a
  footprint's place as `(transform (translate x y) (rotate r) (scale 1 1))` instead of
  `(at x y r)`. Without it every footprint of a KiCad 11 board (the headless `api-server`
  case) was drawn at the origin. A footprint with no `at` takes its position and rotation
  from `transform`.

# kicad-views

MCP Apps views for KiCad MCP Pro: board, schematic, BOM, checks, stackup and parts, built
into one self-contained HTML file (`src/kicad_mcp/data/views/kicad-views.html`) that the
server serves once per view as `ui://kicad/<view>` when `KICAD_MCP_ENABLE_APPS=1`.

The board and schematic are drawn by a vendored copy of
[ecad-viewer](https://github.com/Huaqiu-Electronics/ecad-viewer) (see
`vendor/ecad-viewer/VENDORED.md`). Every operation a view offers is a call to one of the
server's own tools; the views hold no edit logic. Each view also offers tools to the agent
(`focus`, `set_layers`, `select_row`, `capture`, `describe_region`) as the MCP Apps draft's
app-provided tools, and reports what the person selects through `ui/update-model-context`.

```bash
npm ci
npm run build       # writes the bundle into the Python package
npm run typecheck   # checks src/ (the vendored copy is not held to these settings)
```

`dev/harness.py <project folder>` is a minimal MCP Apps host: it runs the server over
stdio and serves a page that opens a view in an iframe, so a view can be developed and
tested in a browser against a real KiCad.

This package is outside the pnpm workspace and has its own `package-lock.json`.

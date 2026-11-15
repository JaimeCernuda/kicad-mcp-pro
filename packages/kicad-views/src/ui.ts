// Small DOM helpers and the views' shared style. No framework: the views are tables,
// toolbars and one viewer element.

import { SAY_WAITING_AFTER_MS, type Bridge } from "./bridge";

export const STYLE = `
:root {
  --kv-bg: var(--color-background-primary, #ffffff);
  --kv-bg2: var(--color-background-secondary, #f4f5f7);
  --kv-fg: var(--color-text-primary, #1d2329);
  --kv-fg2: var(--color-text-secondary, #5b6670);
  --kv-line: var(--color-border-primary, #d9dde2);
  --kv-accent: var(--color-ring-primary, #2f6fdf);
  --kv-danger: #c53a32;
  --kv-warn: #a86700;
  --kv-ok: #2c7a3f;
  --kv-font: var(--font-sans, system-ui, -apple-system, "Segoe UI", sans-serif);
  color-scheme: light;
}
:root[data-theme="dark"] {
  --kv-bg: var(--color-background-primary, #16191d);
  --kv-bg2: var(--color-background-secondary, #1f2328);
  --kv-fg: var(--color-text-primary, #e7eaee);
  --kv-fg2: var(--color-text-secondary, #9aa4ae);
  --kv-line: var(--color-border-primary, #343a41);
  --kv-danger: #ff7b72;
  --kv-warn: #e3b341;
  --kv-ok: #56d364;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: var(--kv-bg); color: var(--kv-fg); font: 13px/1.4 var(--kv-font); }
#kicad-views { display: flex; flex-direction: column; height: 100vh; min-height: 320px; }
.kv-bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 6px 8px; border-bottom: 1px solid var(--kv-line); background: var(--kv-bg2); }
.kv-bar .kv-title { font-weight: 600; margin-right: 8px; }
.kv-bar .kv-spacer { flex: 1; }
button.kv, select.kv, input.kv { font: inherit; color: var(--kv-fg); background: var(--kv-bg); border: 1px solid var(--kv-line); border-radius: 6px; padding: 3px 9px; min-height: 28px; }
button.kv { cursor: pointer; }
button.kv:hover:not(:disabled) { border-color: var(--kv-accent); }
button.kv:disabled { opacity: .5; cursor: default; }
button.kv[aria-pressed="true"] { background: var(--kv-accent); color: #fff; border-color: var(--kv-accent); }
input.kv { min-width: 0; }
.kv-status { padding: 4px 8px; font-size: 12px; color: var(--kv-fg2); border-bottom: 1px solid var(--kv-line); }
.kv-status[data-kind="error"] { color: var(--kv-danger); }
.kv-status[data-kind="busy"]::before { content: "Working: "; font-weight: 600; }
.kv-body { flex: 1; min-height: 0; overflow: auto; position: relative; }
.kv-viewer { position: absolute; inset: 0; }
table.kv { border-collapse: collapse; width: 100%; }
table.kv th, table.kv td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--kv-line); vertical-align: top; }
table.kv th { position: sticky; top: 0; background: var(--kv-bg2); font-weight: 600; z-index: 1; }
table.kv tr[aria-selected="true"] td { background: color-mix(in srgb, var(--kv-accent) 16%, transparent); }
table.kv tr.kv-row { cursor: pointer; }
.kv-tag { display: inline-block; padding: 0 6px; border-radius: 9px; border: 1px solid var(--kv-line); font-size: 11px; }
.kv-tag[data-kind="error"] { color: var(--kv-danger); border-color: var(--kv-danger); }
.kv-tag[data-kind="warning"] { color: var(--kv-warn); border-color: var(--kv-warn); }
.kv-tag[data-kind="ok"] { color: var(--kv-ok); border-color: var(--kv-ok); }
.kv-muted { color: var(--kv-fg2); }
.kv-empty { padding: 24px; color: var(--kv-fg2); }
.kv-group { padding: 6px 8px; font-weight: 600; background: var(--kv-bg2); border-bottom: 1px solid var(--kv-line); }
.kv-drag { position: absolute; border: 2px dashed var(--kv-accent); pointer-events: none; z-index: 5; }
.kv-filters { display: flex; flex-wrap: wrap; gap: 6px; padding: 6px 8px; border-bottom: 1px solid var(--kv-line); }
.kv-filter { display: inline-flex; gap: 4px; align-items: center; }
.kv-compare { display: grid; gap: 0; overflow: auto; border-top: 2px solid var(--kv-line); }
@media (max-width: 520px) { .kv-bar { gap: 4px; } button.kv, select.kv { padding: 2px 6px; } }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
`;

type Child = Node | string | null | undefined | false;

export function h<K extends keyof HTMLElementTagNameMap>(
    tag: K,
    attrs: Record<string, unknown> = {},
    ...children: Child[]
): HTMLElementTagNameMap[K] {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (value === undefined || value === null || value === false) continue;
        if (key.startsWith("on") && typeof value === "function") {
            node.addEventListener(key.slice(2).toLowerCase(), value as EventListener);
        } else if (key === "class") {
            node.className = String(value);
        } else if (key === "dataset") {
            Object.assign(node.dataset, value);
        } else if (value === true) {
            node.setAttribute(key, "");
        } else {
            node.setAttribute(key, String(value));
        }
    }
    for (const child of children) {
        if (child === null || child === undefined || child === false) continue;
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}

export function button(label: string, onClick: () => void, title?: string): HTMLButtonElement {
    return h("button", { class: "kv", type: "button", title: title ?? label, onClick }, label);
}

/** The view's frame: a toolbar, a status line and a body. */
export class Frame {
    readonly bar: HTMLElement;
    readonly status: HTMLElement;
    readonly body: HTMLElement;

    constructor(readonly root: HTMLElement, title: string) {
        const style = h("style");
        style.textContent = STYLE;
        document.head.append(style);
        this.bar = h("div", { class: "kv-bar", role: "toolbar", "aria-label": `${title} actions` }, h("span", { class: "kv-title" }, title));
        this.status = h("div", { class: "kv-status", role: "status", "aria-live": "polite" });
        this.body = h("div", { class: "kv-body" });
        root.append(this.bar, this.status, this.body);
    }

    say(text: string, kind: "info" | "busy" | "error" = "info"): void {
        this.status.textContent = text;
        this.status.dataset.kind = kind;
    }

    /** Run one operation: say what it does, then what came back. */
    async run(label: string, task: () => Promise<string | void>): Promise<void> {
        this.say(label, "busy");
        try {
            const text = await task();
            this.say(text || `${label}: done.`);
        } catch (error) {
            this.say(`${label} failed: ${error instanceof Error ? error.message : String(error)}`, "error");
        }
    }
}

/**
 * Wait for ecad-viewer to finish building a document, however long it takes (a large board
 * parsed while KiCad starts beside it). The status line says it is still drawing and for
 * how long. The wait ends only when the viewer is ready or the element was replaced.
 */
export async function waitForViewer<T>(
    probe: () => T | undefined,
    what: string,
    frame: Frame,
    element: HTMLElement,
): Promise<T> {
    const started = Date.now();
    let said = -1;
    for (;;) {
        const found = probe();
        if (found) return found;
        if (!element.isConnected) throw new Error(`The ${what} was replaced before it finished drawing.`);
        const waited = Date.now() - started;
        const seconds = Math.floor(waited / 1000);
        if (waited >= SAY_WAITING_AFTER_MS && seconds !== said) {
            said = seconds;
            frame.say(`Drawing the ${what}\u2026 (${seconds} s)`, "busy");
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
}

/** Follow the board's revision: call ``onChange`` whenever it moves past the one shown. */
export function followRevisions(
    bridge: Bridge,
    project: Record<string, unknown>,
    getShown: () => number,
    onChange: (revision: number) => Promise<void>,
): () => void {
    let stopped = false;
    const loop = async () => {
        while (!stopped) {
            try {
                const answer = await bridge.callData<{ revision: number; changed: boolean }>(
                    "view_wait_revision",
                    { ...project, since: getShown(), timeout_s: 25 },
                );
                if (!stopped && answer.changed && answer.revision > getShown()) await onChange(answer.revision);
            } catch {
                await new Promise((resolve) => setTimeout(resolve, 3000));
            }
        }
    };
    void loop();
    return () => {
        stopped = true;
    };
}

export function dataUrlToBase64(url: string): string {
    return url.slice(url.indexOf(",") + 1);
}

const detached = document.implementation.createHTMLDocument("");

/**
 * Create an ecad-viewer element with attributes. Its custom elements set attributes in
 * their constructors, which ``document.createElement`` forbids; an element made in a
 * detached document and adopted here is upgraded when attached, where that is allowed.
 */
export function ecadElement(tag: string, attrs: Record<string, string> = {}): HTMLElement {
    const node = detached.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
    return document.adoptNode(node) as HTMLElement;
}

/** What a shared selection (the workbench's /pcb/selection) names, from whichever view. */
export interface SharedTarget {
    references: string[];
    position_mm?: { x: number; y: number };
    part?: string;
}

export function sharedTarget(selection: unknown): SharedTarget {
    const out: SharedTarget = { references: [] };
    if (!selection || typeof selection !== "object") return out;
    const sel = selection as Record<string, unknown>;
    const refs = new Set<string>();
    const add = (value: unknown) => {
        if (typeof value === "string" && value) refs.add(value.split(".")[0]!);
    };
    add(sel.reference);
    if (Array.isArray(sel.references)) sel.references.forEach(add);
    const where = sel.position_mm as { x?: unknown; y?: unknown } | undefined;
    if (where && typeof where.x === "number" && typeof where.y === "number") out.position_mm = { x: where.x, y: where.y };
    const part = sel.part as Record<string, unknown> | undefined;
    if (part && typeof part === "object") out.part = String(part.lcsc || part.mpn || "");
    out.references = [...refs];
    return out;
}

export const SELECT_SCHEMA = {
    type: "object",
    properties: {
        selection: {
            type: "object",
            description: "A selection another view reported (references, a position or a part).",
        },
    },
    required: ["selection"],
};

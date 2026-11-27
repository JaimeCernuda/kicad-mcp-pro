// One bundle, six views (KV2): the server serves this page once per view and sets
// window.KICAD_VIEW to which one.

import "./vendor-setup";
import { Bridge, messageOf, textOf } from "./bridge";
import { boardView } from "./views/board";
import { bomView } from "./views/bom";
import { checksView } from "./views/checks";
import { partsView } from "./views/parts";
import { schematicView } from "./views/schematic";
import { stackupView } from "./views/stackup";

declare global {
    interface Window {
        KICAD_VIEW?: string;
    }
}

const VIEWS = {
    board: boardView,
    schematic: schematicView,
    bom: bomView,
    checks: checksView,
    stackup: stackupView,
    parts: partsView,
} as const;

async function main(): Promise<void> {
    const root = document.getElementById("kicad-views")!;
    const bridge = new Bridge();
    const name = (window.KICAD_VIEW ?? "board") as keyof typeof VIEWS;
    const view = VIEWS[name] ?? boardView;
    // The host sends the opening call's result after initialization; wait for it, saying so.
    const opened = new Promise<void>((resolve) => {
        bridge.on("ui/notifications/tool-result", () => resolve());
    });
    const started = Date.now();
    const waitingForResult = window.setInterval(() => {
        const seconds = Math.floor((Date.now() - started) / 1000);
        if (seconds >= 3) root.textContent = `Waiting for KiCad to open the ${name} view\u2026 (${seconds} s)`;
    }, 1000);
    void opened.then(() => {
        window.clearInterval(waitingForResult);
        root.textContent = "";
    });
    try {
        await bridge.initialize(`kicad-${name}`, "0.1.0");
    } catch (error) {
        root.textContent = `The host did not start the view: ${messageOf(error)}`;
        return;
    }
    // The opening call's arguments: the project and the view's own (KV9 reopening).
    await opened;
    if (bridge.toolResult?.isError) {
        root.textContent = textOf(bridge.toolResult) || "KiCad could not open this view.";
        return;
    }
    const structured = (bridge.toolResult?.structuredContent ?? {}) as Record<string, unknown>;
    const args = { ...((structured.arguments as Record<string, unknown>) ?? {}), ...bridge.toolInput };
    if (name !== "checks") void bridge.requestFullscreen();
    // A data call still unanswered after a few seconds shows in the view's status line.
    // The line goes back to what it said once the call answers, unless the view wrote it since.
    let before: { text: string; kind: string | undefined; shown: string } | null = null;
    bridge.waiting = (seconds) => {
        const line = root.querySelector<HTMLElement>(".kv-status");
        if (!line) return;
        if (before === null) before = { text: line.textContent ?? "", kind: line.dataset.kind, shown: "" };
        line.textContent = `Waiting for KiCad\u2026 (${seconds} s)`;
        line.dataset.kind = "busy";
        before.shown = line.textContent;
    };
    bridge.waited = () => {
        const line = root.querySelector<HTMLElement>(".kv-status");
        if (line && before && line.textContent === before.shown) {
            line.textContent = before.text;
            if (before.kind === undefined) delete line.dataset.kind;
            else line.dataset.kind = before.kind;
        }
        before = null;
    };
    try {
        await view(bridge, root, args);
    } catch (error) {
        const note = document.createElement("div");
        note.className = "kv-status";
        note.dataset.kind = "error";
        note.textContent = `The ${name} view could not load: ${messageOf(error)}`;
        root.prepend(note);
    }
}

void main();

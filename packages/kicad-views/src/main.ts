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
    const opened = new Promise<void>((resolve) => {
        bridge.on("ui/notifications/tool-result", () => resolve());
        setTimeout(resolve, 4000);
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

// The board view (KV10): ecad-viewer draws the board; every operation is a server tool.

import { textOf, textResult, type Bridge, type ToolResult } from "../bridge";
import { button, dataUrlToBase64, ecadElement, followRevisions, Frame, h, SELECT_SCHEMA, sharedTarget } from "../ui";
import { regionOnBoard, type Rect } from "./region";

interface BoardData {
    revision: number;
    board_name: string;
    text: string;
    source: string;
    note?: string;
}

type AnyViewer = any;

interface Selection {
    kind: "footprint" | "pad" | "track" | "via" | "net" | "zone";
    reference?: string;
    net?: string;
    layer?: string;
    position_mm?: { x: number; y: number };
    locked?: boolean;
    rotation_deg?: number;
}

/** A pad's position on the board: KiCad keeps it relative to its footprint, in the
 * footprint's frame, rotated counter-clockwise on screen with y pointing down. */
export function padOnBoard(pad: { x: number; y: number }, at: { x: number; y: number }, rotationDeg: number): { x: number; y: number } {
    const a = (rotationDeg * Math.PI) / 180;
    return { x: at.x + pad.x * Math.cos(a) + pad.y * Math.sin(a), y: at.y - pad.x * Math.sin(a) + pad.y * Math.cos(a) };
}

/** A footprint's box on the board from its pads (ecad-viewer's own footprint box is wrong
 * for footprints drawn from pads only), with its origin when it has none. */
export function footprintBox(fp: any): { x: number; y: number; w: number; h: number } | null {
    const at = fp?.at?.position;
    if (!at) return null;
    const rotation = Number(fp.at?.rotation ?? 0);
    const pads: any[] = fp.pads ?? [...(fp.items?.() ?? [])].filter((item: any) => item?.typeId === "Pad");
    let [x1, y1, x2, y2] = [at.x, at.y, at.x, at.y];
    for (const pad of pads) {
        const local = pad?.at?.position;
        if (!local) continue;
        const centre = padOnBoard(local, at, rotation);
        const r = Math.max(Number(pad.size?.x ?? 0), Number(pad.size?.y ?? 0)) / 2;
        [x1, y1] = [Math.min(x1, centre.x - r), Math.min(y1, centre.y - r)];
        [x2, y2] = [Math.max(x2, centre.x + r), Math.max(y2, centre.y + r)];
    }
    return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

function itemSelection(raw: any, viewer: AnyViewer): Selection | null {
    if (!raw) return null;
    const item = raw.item ?? raw;
    const type = item?.typeId as string | undefined;
    const pos = item?.at?.position ?? item?.position;
    const where = pos ? { x: round(pos.x), y: round(pos.y) } : undefined;
    const netName = (number: unknown) =>
        typeof number === "number" && viewer?.board?.getNetName ? viewer.board.getNetName(number) : undefined;
    if (type === "Footprint") {
        return {
            kind: "footprint",
            reference: item.reference,
            layer: item.layer,
            position_mm: where,
            locked: Boolean(item.locked),
            rotation_deg: Number(item.at?.rotation ?? 0),
        };
    }
    if (type === "Pad") {
        const parent = item.parent?.at?.position;
        const onBoard = pos && parent ? padOnBoard(pos, parent, Number(item.parent.at?.rotation ?? 0)) : pos;
        return {
            kind: "pad",
            reference: item.parent?.reference ? `${item.parent.reference}.${item.number}` : String(item.number ?? ""),
            net: item.net?.name ?? netName(item.net?.number),
            layer: item.parent?.layer,
            position_mm: onBoard ? { x: round(onBoard.x), y: round(onBoard.y) } : undefined,
        };
    }
    if (type === "LineSegment" || type === "ArcSegment") {
        const start = item.start;
        return {
            kind: "track",
            net: netName(item.net),
            layer: item.layer,
            position_mm: start ? { x: round(start.x), y: round(start.y) } : undefined,
        };
    }
    if (type === "Via") return { kind: "via", net: netName(item.net), position_mm: where };
    if (raw.net && typeof raw.net === "string") return { kind: "net", net: raw.net };
    return null;
}

function round(value: number): number {
    return Math.round(value * 1000) / 1000;
}

function describe(selection: Selection | null): string {
    if (!selection) return "Nothing is selected on the board.";
    const parts = [`Selected on the board: ${selection.kind}`];
    if (selection.reference) parts.push(selection.reference);
    if (selection.net) parts.push(`net ${selection.net}`);
    if (selection.layer) parts.push(`on ${selection.layer}`);
    if (selection.position_mm) parts.push(`at (${selection.position_mm.x}, ${selection.position_mm.y}) mm`);
    if (selection.locked) parts.push("(locked)");
    return parts.join(" ");
}

/** How long the board view waits for the 3D export (the server allows kicad-cli 180 s). */
const GLB_TIMEOUT_MS = 240_000;

/** The first element named ``tag`` under ``root``, looking inside shadow roots. */
function deepFind(root: Element | ShadowRoot | null, tag: string): Element | null {
    if (!root) return null;
    if (root instanceof Element && root.shadowRoot) {
        const inside = deepFind(root.shadowRoot, tag);
        if (inside) return inside;
    }
    for (const el of root.querySelectorAll("*")) {
        if (el.tagName === tag) return el;
        const inner = el.shadowRoot ? deepFind(el.shadowRoot, tag) : null;
        if (inner) return inner;
    }
    return null;
}

function nextFrame(): Promise<void> {
    return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
}

/** Decode base64 without a callback per character (a model is several megabytes). */
export function base64Bytes(text: string): Uint8Array<ArrayBuffer> {
    const binary = atob(text);
    const bytes = new Uint8Array(new ArrayBuffer(binary.length));
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
}

export async function boardView(bridge: Bridge, root: HTMLElement, project: Record<string, unknown>): Promise<void> {
    const frame = new Frame(root, "Board");
    const host = h("div", { class: "kv-viewer" });
    frame.body.append(host);
    let shown = 0;
    let element: HTMLElement | null = null;
    let viewer: AnyViewer = null;
    let selection: Selection | null = null;
    let moveMode = false;
    // The 3D model: one export kept as bytes; each ecad-viewer element gets its own URL,
    // released when the element goes (an edit replaces the element).
    let glb: ArrayBuffer | null = null;
    let elementGlbUrl: string | null = null;
    // The 3D viewer parses a model registered here from memory instead of fetching its
    // URL, which a host's Content Security Policy can block (vendored viewer.ts).
    const models = ((globalThis as { __ecadModelBytes?: Map<string, ArrayBuffer> }).__ecadModelBytes ??=
        new Map<string, ArrayBuffer>());
    const modelUrl = (bytes: ArrayBuffer): string => {
        const url = URL.createObjectURL(new Blob([bytes], { type: "model/gltf-binary" }));
        models.set(url, bytes);
        return url;
    };
    const releaseModel = (url: string | null): void => {
        if (!url) return;
        models.delete(url);
        URL.revokeObjectURL(url);
    };
    // Fit again when the view changes size, until the person moves the board themselves.
    let autoFit = true;
    const buttons: Record<string, HTMLButtonElement> = {};

    const footprintBy = (reference: string): any =>
        (viewer?.board?.footprints ?? []).find((fp: any) => fp.reference === reference);

    const select = (next: Selection | null) => {
        selection = next;
        const fp = next?.kind === "footprint";
        buttons.rotate!.disabled = !fp;
        buttons.flip!.disabled = !fp;
        buttons.lock!.disabled = !fp;
        buttons.lock!.textContent = next?.locked ? "Unlock" : "Lock";
        buttons.move!.disabled = !fp;
        buttons.highlight!.disabled = !next?.net;
        bridge.report("selection", next, describe(next));
        frame.say(describe(next));
    };

    const reportLayers = () => {
        const visible: string[] = [];
        for (const layer of viewer?.layers?.in_ui_order?.() ?? []) if (layer.visible) visible.push(layer.name);
        bridge.report("layers", visible, `Visible board layers: ${visible.join(", ") || "none"}.`);
    };

    const waitViewer = async (target: any): Promise<AnyViewer> => {
        for (let i = 0; i < 300; i += 1) {
            const found = target?.board_viewer;
            if (found?.board) return found;
            await new Promise((resolve) => setTimeout(resolve, 50));
        }
        throw new Error("The board did not finish loading.");
    };

    const load = async (keepCamera: boolean) => {
        const data = await bridge.callData<BoardData>("view_data_board", project);
        const camera = keepCamera && viewer?.viewport?.camera;
        const saved = camera ? { center: camera.center.copy?.() ?? camera.center, zoom: camera.zoom } : null;
        const next = ecadElement("ecad-viewer", { style: "position:absolute;inset:0;height:auto;max-height:none;aspect-ratio:auto" });
        next.append(ecadElement("ecad-blob", { filename: data.board_name, content: data.text }));
        const nextGlbUrl = glb ? modelUrl(glb) : null;
        if (nextGlbUrl) next.append(ecadElement("ecad-3d-source", { src: nextGlbUrl }));
        host.append(next);
        const nextViewer = await waitViewer(next);
        element?.remove();
        releaseModel(elementGlbUrl);
        element = next;
        elementGlbUrl = nextGlbUrl;
        viewer = nextViewer;
        if (saved && viewer.viewport?.camera) {
            viewer.viewport.camera.center = saved.center;
            viewer.viewport.camera.zoom = saved.zoom;
            viewer.draw();
        } else {
            await nextFrame();
            fitBoard();
        }
        viewer.addEventListener("kicanvas:select", (event: any) => select(itemSelection(event.detail?.item, viewer)));
        shown = data.revision;
        bridge.report("revision", shown, `The board view shows revision ${shown}.`);
        reportLayers();
        if (selection?.reference && selection.kind === "footprint") {
            const fp = footprintBy(selection.reference);
            if (fp) select(itemSelection(fp, viewer));
        }
        const source = data.source === "live" ? "from the live KiCad" : "from the board file";
        frame.say(`Revision ${shown}, ${source}.${data.note ? ` ${data.note}` : ""}`);
    };

    const after = async (label: string, call: () => Promise<ToolResult>) => {
        await frame.run(label, async () => {
            const result = await call();
            const text = textOf(result);
            if (result.isError) throw new Error(text || label);
            await load(true);
            return text;
        });
    };

    const footprintCall = (name: string, args: (fp: Selection) => Record<string, unknown>, label: string) => () => {
        if (selection?.kind !== "footprint" || !selection.reference) return;
        const current = selection;
        void after(`${label} ${current.reference}`, () => bridge.callTool(name, args(current)));
    };

    buttons.move = button("Move", () => {
        moveMode = !moveMode;
        buttons.move!.setAttribute("aria-pressed", String(moveMode));
        overlay.hidden = !moveMode;
        frame.say(moveMode ? "Drag the selected footprint to its new place." : describe(selection));
    }, "Drag the selected footprint");
    buttons.rotate = button(
        "Rotate 90°",
        footprintCall(
            "pcb_move_footprint",
            (fp) => ({
                reference: fp.reference,
                x_mm: fp.position_mm?.x,
                y_mm: fp.position_mm?.y,
                rotation_deg: ((fp.rotation_deg ?? 0) + 90) % 360,
            }),
            "Rotate",
        ),
    );
    buttons.flip = button(
        "Other side",
        footprintCall(
            "pcb_set_footprint_layer",
            (fp) => ({ reference: fp.reference, layer: fp.layer === "B.Cu" ? "F_Cu" : "B_Cu" }),
            "Move to the other side:",
        ),
    );
    buttons.lock = button(
        "Lock",
        footprintCall("pcb_set_footprint_locked", (fp) => ({ reference: fp.reference, locked: !fp.locked }), "Lock or unlock"),
    );
    buttons.highlight = button("Highlight in KiCad", () => {
        if (selection?.net) void after(`Highlight ${selection.net}`, () => bridge.callTool("pcb_highlight_net", { net_name: selection!.net }));
    });
    const refill = button("Refill zones", () => void after("Refill zones", () => bridge.callTool("pcb_refill_zones")));
    const drc = button("Run DRC", () =>
        void frame.run("Run DRC", async () => {
            const result = await bridge.callTool("run_drc");
            const summary = (result.structuredContent as any)?.summary;
            return typeof summary === "string" ? summary : textOf(result).split("\n")[0];
        }),
    );
    const fit = button("Fit", () => fitBoard());
    frame.bar.append(
        buttons.move,
        buttons.rotate,
        buttons.flip,
        buttons.lock,
        buttons.highlight,
        h("span", { class: "kv-spacer" }),
        refill,
        drc,
        fit,
    );

    // The drag overlay: one call when the drag ends, never one per pointer move.
    const overlay = h("div", { class: "kv-viewer", style: "z-index:4;cursor:grab", hidden: true });
    const ghost = h("div", { class: "kv-drag", hidden: true });
    frame.body.append(overlay, ghost);
    let dragStart: { x: number; y: number; world: { x: number; y: number } } | null = null;
    overlay.addEventListener("pointerdown", (event) => {
        if (selection?.kind !== "footprint" || !viewer) return;
        if (selection.locked) {
            frame.say(`${selection.reference} is locked; unlock it to move it.`, "error");
            return;
        }
        overlay.setPointerCapture(event.pointerId);
        dragStart = { x: event.clientX, y: event.clientY, world: viewer.screenToWorld(event.clientX, event.clientY) };
        const fp = footprintBy(selection.reference!);
        const box = footprintBox(fp);
        if (box) {
            const a = viewer.worldToScreen(box.x, box.y);
            const b = viewer.worldToScreen(box.x + box.w, box.y + box.h);
            const bounds = frame.body.getBoundingClientRect();
            Object.assign(ghost.style, {
                left: `${Math.min(a.x, b.x) - bounds.left}px`,
                top: `${Math.min(a.y, b.y) - bounds.top}px`,
                width: `${Math.abs(b.x - a.x)}px`,
                height: `${Math.abs(b.y - a.y)}px`,
                transform: "translate(0px, 0px)",
            });
            ghost.hidden = false;
        }
    });
    overlay.addEventListener("pointermove", (event) => {
        if (!dragStart) return;
        ghost.style.transform = `translate(${event.clientX - dragStart.x}px, ${event.clientY - dragStart.y}px)`;
    });
    overlay.addEventListener("pointerup", (event) => {
        if (!dragStart || !selection?.position_mm) return;
        const end = viewer.screenToWorld(event.clientX, event.clientY);
        const dx = end.x - dragStart.world.x;
        const dy = end.y - dragStart.world.y;
        dragStart = null;
        ghost.hidden = true;
        if (Math.abs(dx) < 0.01 && Math.abs(dy) < 0.01) return;
        const current = selection;
        void after(`Move ${current.reference}`, () =>
            bridge.callTool("pcb_move_footprint", {
                reference: current.reference,
                x_mm: round(current.position_mm!.x + dx),
                y_mm: round(current.position_mm!.y + dy),
            }),
        );
    });

    // The agent's tools on this view (KV8).
    bridge.tools.set("focus", {
        name: "focus",
        description:
            "Centre the board view on a footprint reference (U1), a net name (GND) or a point {x_mm, y_mm}, and highlight it.",
        inputSchema: {
            type: "object",
            properties: {
                target: { type: "string", description: "A reference or a net name" },
                x_mm: { type: "number" },
                y_mm: { type: "number" },
            },
        },
        run: async (args) => focus(args),
    });
    bridge.tools.set("select", {
        name: "select",
        description: "Show on the board what another view selected: footprints by reference, or a point.",
        inputSchema: SELECT_SCHEMA,
        run: async (args) => {
            const target = sharedTarget(args.selection);
            const first = target.references.find((ref) => footprintBy(ref));
            if (first) return focus({ target: first });
            if (target.position_mm) {
                viewer.viewport.camera.center.set?.(target.position_mm.x, target.position_mm.y);
                viewer.viewport.camera.zoom = Math.max(viewer.viewport.camera.zoom, 8);
                viewer.draw();
                return textResult(`Centred on (${target.position_mm.x}, ${target.position_mm.y}) mm.`);
            }
            return textResult("Nothing on the board matches that selection.");
        },
    });
    bridge.tools.set("set_layers", {
        name: "set_layers",
        description: "Show only these board layers (KiCad names such as F.Cu, B.Cu, Edge.Cuts, F.Silkscreen).",
        inputSchema: {
            type: "object",
            properties: { layers: { type: "array", items: { type: "string" } } },
            required: ["layers"],
        },
        run: async (args) => {
            const wanted = new Set((args.layers as string[]) ?? []);
            for (const layer of viewer?.layers?.in_ui_order?.() ?? []) layer.visible = wanted.has(layer.name);
            viewer?.draw();
            reportLayers();
            return textResult(`Visible layers: ${[...wanted].join(", ")}.`);
        },
    });
    bridge.tools.set("show", {
        name: "show",
        description:
            "Switch the board view between the board drawing (mode board) and the 3D model with component bodies (mode 3d); in 3D it returns a PNG of what the view shows.",
        inputSchema: {
            type: "object",
            properties: { mode: { type: "string", enum: ["board", "3d"] } },
            required: ["mode"],
        },
        run: async (args) => showMode(String(args.mode ?? "board")),
    });
    bridge.tools.set("capture", {
        name: "capture",
        description: "Return a PNG of what the board view shows now.",
        inputSchema: { type: "object", properties: {} },
        run: async () => {
            const png = await capture();
            return { content: [{ type: "image", data: png, mimeType: "image/png" }, { type: "text", text: `Board view at revision ${shown}.` }] };
        },
    });
    bridge.tools.set("describe_region", {
        name: "describe_region",
        description:
            "Describe a rectangle of the view in CSS pixels: a PNG of that region and the footprints, pads, tracks and nets under it, with layers and millimetre coordinates.",
        inputSchema: {
            type: "object",
            properties: { x: { type: "number" }, y: { type: "number" }, width: { type: "number" }, height: { type: "number" } },
            required: ["x", "y", "width", "height"],
        },
        run: async (args) => {
            const rect = args as unknown as Rect;
            const png = await capture(rect);
            const found = regionOnBoard(viewer, rect, root.getBoundingClientRect());
            return {
                content: [
                    { type: "image", data: png, mimeType: "image/png" },
                    { type: "text", text: JSON.stringify(found) },
                ],
                structuredContent: found as unknown as Record<string, unknown>,
            };
        },
    });

    /** The ecad-viewer tab button (PCB, 3D, BOM) of the element shown now. */
    function tabButton(kind: string): HTMLElement | null {
        const header = deepFind(element, "TAB-HEADER");
        const tabs = [...(header?.shadowRoot?.querySelectorAll("tab-button") ?? [])] as HTMLElement[];
        return tabs.find((tab) => tab.textContent?.trim() === kind) ?? null;
    }

    /** Switch the view between the board drawing and the 3D model, as the tabs do. */
    async function showMode(mode: string): Promise<ToolResult> {
        const kind = mode === "3d" ? "3D" : "PCB";
        const tab = tabButton(kind);
        if (!tab) throw new Error(`The view has no ${kind} tab.`);
        tab.click();
        if (kind === "PCB") {
            await nextFrame();
            fitBoard();
            return textResult("Showing the board drawing.");
        }
        for (let i = 0; i < 120; i += 1) {
            const viewer3d = deepFind(element, "ECAD-3D-VIEWER") as any;
            if (viewer3d?._loaded && viewer3d.style.display !== "none") {
                await nextFrame();
                const container = viewer3d._viewer_container;
                container?.render?.();
                const canvas = container?.renderer?.domElement as HTMLCanvasElement | undefined;
                const png = canvas ? dataUrlToBase64(canvas.toDataURL("image/png")) : "";
                const text = "Showing the board in 3D (the person can turn it with the mouse).";
                return png
                    ? { content: [{ type: "image", data: png, mimeType: "image/png" }, { type: "text", text }] }
                    : textResult(text);
            }
            await new Promise((resolve) => setTimeout(resolve, 500));
        }
        throw new Error(glb ? "The 3D model did not finish loading." : "The 3D model is not exported yet.");
    }

    /** Frame the whole board outline. ecad-viewer's zoom_to_board only sets the camera,
     * so the board is drawn again here. */
    function fitBoard(): void {
        if (!viewer) return;
        viewer.zoom_to_board?.();
        viewer.draw?.();
        autoFit = true;
    }

    // The view settles its size after the board is drawn (the host's display mode, a
    // fullscreen request): fit again then, unless the person has moved the board.
    for (const kind of ["pointerdown", "wheel", "keydown"]) {
        host.addEventListener(kind, () => (autoFit = false), { capture: true, passive: true });
    }
    let lastSize = "";
    new ResizeObserver(() => {
        const box = host.getBoundingClientRect();
        const size = `${Math.round(box.width)}x${Math.round(box.height)}`;
        if (size === lastSize) return;
        lastSize = size;
        if (autoFit) requestAnimationFrame(() => fitBoard());
    }).observe(host);

    async function capture(rect?: Rect): Promise<string> {
        if (!viewer?.canvas) throw new Error("The board is not drawn yet.");
        viewer.draw();
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const canvas = viewer.canvas as HTMLCanvasElement;
        if (!rect) return dataUrlToBase64(canvas.toDataURL("image/png"));
        const scale = canvas.width / canvas.getBoundingClientRect().width;
        const offset = canvas.getBoundingClientRect();
        const base = root.getBoundingClientRect();
        const crop = document.createElement("canvas");
        crop.width = Math.max(1, Math.round(rect.width * scale));
        crop.height = Math.max(1, Math.round(rect.height * scale));
        crop.getContext("2d")!.drawImage(
            canvas,
            (rect.x + base.left - offset.left) * scale,
            (rect.y + base.top - offset.top) * scale,
            rect.width * scale,
            rect.height * scale,
            0,
            0,
            crop.width,
            crop.height,
        );
        return dataUrlToBase64(crop.toDataURL("image/png"));
    }

    function focus(args: Record<string, unknown>): ToolResult {
        if (!viewer) throw new Error("The board is not drawn yet.");
        const target = String(args.target ?? "").trim();
        if (target) {
            const fp = footprintBy(target);
            if (fp) {
                const box = footprintBox(fp);
                const camera = viewer.viewport.camera;
                if (box && camera?.bbox) {
                    const margin = Math.max(3, Math.max(box.w, box.h) * 1.5);
                    const BBox = camera.bbox.constructor;
                    camera.bbox = new BBox(box.x - margin, box.y - margin, box.w + 2 * margin, box.h + 2 * margin);
                }
                viewer.draw();
                select(itemSelection(fp, viewer));
                return textResult(`Focused ${target}.`);
            }
            const net = (viewer.board?.nets ?? []).find((n: any) => n.name === target);
            if (net) {
                viewer.focus_net(net.number);
                select({ kind: "net", net: target });
                return textResult(`Focused net ${target}.`);
            }
            throw new Error(`No footprint or net named ${target} on this board.`);
        }
        if (typeof args.x_mm === "number" && typeof args.y_mm === "number") {
            viewer.viewport.camera.center.set?.(args.x_mm, args.y_mm);
            viewer.draw();
            return textResult(`Centred on (${args.x_mm}, ${args.y_mm}) mm.`);
        }
        throw new Error("Give a target (reference or net) or x_mm and y_mm.");
    }

    bridge.toolsChanged();
    frame.say("Loading the board…", "busy");
    await load(false);
    if (typeof project.focus === "string" && project.focus) {
        try {
            focus({ target: project.focus });
        } catch (error) {
            frame.say(String(error), "error");
        }
    }
    if (Array.isArray(project.layers) && project.layers.length) {
        await bridge.tools.get("set_layers")!.run({ layers: project.layers });
    }
    // The 3D model for the 3D tab (KQ15): exported once, and again after the board
    // changes. The tab reads its model when it opens, so a model that arrives later is
    // announced to it (3d:url:ready); a model that fails to load says why.
    window.addEventListener("3d:viewer:failed", (event) =>
        frame.say(`The 3D model could not be shown: ${(event as CustomEvent<string>).detail}`, "error"),
    );
    let glbBusy = false;
    let glbAgain = false;
    const refreshGlb = async (): Promise<void> => {
        if (glbBusy) {
            glbAgain = true;
            return;
        }
        glbBusy = true;
        try {
            const data = await bridge.callData<{ glb_base64: string }>("view_data_glb", project, GLB_TIMEOUT_MS);
            glb = base64Bytes(data.glb_base64).buffer;
            const url = modelUrl(glb);
            releaseModel(elementGlbUrl);
            elementGlbUrl = url;
            const ecad = element as any;
            if (ecad?.project) ecad.project.ov_3d_url = url;
            window.dispatchEvent(new CustomEvent("3d:url:ready", { detail: url }));
        } catch (error) {
            frame.say(`3D is not available: ${error instanceof Error ? error.message : String(error)}`, "error");
        } finally {
            glbBusy = false;
            if (glbAgain) {
                glbAgain = false;
                void refreshGlb();
            }
        }
    };
    void refreshGlb().then(() => {
        if (project.mode === "3d") void showMode("3d").catch((error) => frame.say(String(error), "error"));
    });
    followRevisions(bridge, project, () => shown, async () => {
        await load(true);
        void refreshGlb();
    });
}

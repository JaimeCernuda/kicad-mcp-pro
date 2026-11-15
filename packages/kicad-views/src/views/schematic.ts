// The schematic view (KV11): ecad-viewer draws every sheet; "not fitted" is sch_set_dnp.

import { textOf, textResult, type Bridge } from "../bridge";
import { button, dataUrlToBase64, ecadElement, followRevisions, Frame, h, SELECT_SCHEMA, sharedTarget } from "../ui";
import { regionOnSchematic, type Rect } from "./region";

interface SchematicData {
    revision: number;
    root: string;
    files: Array<{ name: string; text: string }>;
}

function symbolReference(symbol: any): string | undefined {
    return (
        symbol?.reference ??
        symbol?.default_instance?.reference ??
        symbol?.properties?.get?.("Reference")?.text ??
        undefined
    );
}

export async function schematicView(bridge: Bridge, root: HTMLElement, project: Record<string, unknown>): Promise<void> {
    const frame = new Frame(root, "Schematic");
    const host = h("div", { class: "kv-viewer" });
    frame.body.append(host);
    let shown = 0;
    let element: any = null;
    let viewer: any = null;
    let selected: { reference: string; value?: string; dnp: boolean; sheet?: string } | null = null;

    const dnp = button("Not fitted", () => {
        if (!selected) return;
        const target = selected;
        void frame.run(`${target.dnp ? "Fit" : "Mark not fitted"} ${target.reference}`, async () => {
            const result = await bridge.callTool("sch_set_dnp", { reference: target.reference, enabled: !target.dnp });
            if (result.isError) throw new Error(textOf(result));
            await load();
            return textOf(result);
        });
    });
    dnp.disabled = true;
    frame.bar.append(dnp, h("span", { class: "kv-spacer" }), button("Fit", () => viewer?.zoom_fit_top_item?.()));

    const select = (symbol: any) => {
        const reference = symbolReference(symbol);
        if (!reference) return;
        selected = { reference, value: symbol.value ?? symbol.default_instance?.value, dnp: Boolean(symbol.dnp), sheet: viewer?.schematic?.filename };
        dnp.disabled = false;
        dnp.textContent = selected.dnp ? "Fit it again" : "Not fitted";
        const text = `Selected in the schematic: symbol ${reference}${selected.value ? ` (${selected.value})` : ""} on sheet ${selected.sheet ?? "?"}${selected.dnp ? ", not fitted" : ""}.`;
        bridge.report("selection", { kind: "symbol", ...selected }, text);
        frame.say(text);
    };

    const waitViewer = async (target: any) => {
        for (let i = 0; i < 300; i += 1) {
            if (target?.schematic_viewer?.schematic) return target.schematic_viewer;
            await new Promise((resolve) => setTimeout(resolve, 50));
        }
        throw new Error("The schematic did not finish loading.");
    };

    const load = async () => {
        const data = await bridge.callData<SchematicData>("view_data_schematic", project);
        const next = ecadElement("ecad-viewer", { style: "position:absolute;inset:0" }) as any;
        for (const file of data.files) next.append(ecadElement("ecad-blob", { filename: file.name, content: file.text }));
        host.append(next);
        const nextViewer = await waitViewer(next);
        element?.remove();
        element = next;
        viewer = nextViewer;
        viewer.addEventListener("kicanvas:select", (event: any) => {
            const item = event.detail?.item;
            if (item && (symbolReference(item) || item.lib_id)) select(item);
        });
        shown = data.revision;
        bridge.report("revision", shown, `The schematic view shows revision ${shown}.`);
        frame.say(`${data.files.length} sheet file${data.files.length === 1 ? "" : "s"}, revision ${shown}.`);
    };

    const focus = (reference: string) => {
        window.dispatchEvent(new CustomEvent("sch:select:designator", { detail: { designator: reference } }));
        for (const sheet of [viewer?.schematic]) {
            const symbols = sheet?.symbols instanceof Map ? [...sheet.symbols.values()] : [];
            const found = symbols.find((symbol: any) => symbolReference(symbol) === reference);
            if (found) select(found);
        }
    };

    bridge.tools.set("focus", {
        name: "focus",
        description: "Show and select a symbol by reference (U1), switching to its sheet.",
        inputSchema: { type: "object", properties: { target: { type: "string" } }, required: ["target"] },
        run: async (args) => {
            focus(String(args.target));
            return textResult(`Focused ${String(args.target)} in the schematic.`);
        },
    });
    bridge.tools.set("select", {
        name: "select",
        description: "Show the symbol another view selected.",
        inputSchema: SELECT_SCHEMA,
        run: async (args) => {
            const ref = sharedTarget(args.selection).references[0];
            if (!ref) return textResult("That selection names no symbol.");
            focus(ref);
            return textResult(`Focused ${ref}.`);
        },
    });
    bridge.tools.set("capture", {
        name: "capture",
        description: "Return a PNG of the schematic sheet the view shows now.",
        inputSchema: { type: "object", properties: {} },
        run: async () => ({ content: [{ type: "image", data: await capture(), mimeType: "image/png" }] }),
    });
    bridge.tools.set("describe_region", {
        name: "describe_region",
        description: "Describe a rectangle of the view in CSS pixels: a PNG of it and the symbols, pins and nets under it.",
        inputSchema: {
            type: "object",
            properties: { x: { type: "number" }, y: { type: "number" }, width: { type: "number" }, height: { type: "number" } },
            required: ["x", "y", "width", "height"],
        },
        run: async (args) => {
            const rect = args as unknown as Rect;
            const found = regionOnSchematic(viewer, rect, root.getBoundingClientRect());
            return {
                content: [
                    { type: "image", data: await capture(rect), mimeType: "image/png" },
                    { type: "text", text: JSON.stringify(found) },
                ],
                structuredContent: found as unknown as Record<string, unknown>,
            };
        },
    });

    async function capture(rect?: Rect): Promise<string> {
        if (!viewer?.canvas) throw new Error("The schematic is not drawn yet.");
        viewer.draw();
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const canvas = viewer.canvas as HTMLCanvasElement;
        if (!rect) return dataUrlToBase64(canvas.toDataURL("image/png"));
        const box = canvas.getBoundingClientRect();
        const base = root.getBoundingClientRect();
        const scale = canvas.width / box.width;
        const crop = document.createElement("canvas");
        crop.width = Math.max(1, Math.round(rect.width * scale));
        crop.height = Math.max(1, Math.round(rect.height * scale));
        crop.getContext("2d")!.drawImage(
            canvas,
            (rect.x + base.left - box.left) * scale,
            (rect.y + base.top - box.top) * scale,
            rect.width * scale,
            rect.height * scale,
            0,
            0,
            crop.width,
            crop.height,
        );
        return dataUrlToBase64(crop.toDataURL("image/png"));
    }

    bridge.toolsChanged();
    frame.say("Loading the schematic…", "busy");
    await load();
    if (typeof project.focus === "string" && project.focus) focus(project.focus);
    followRevisions(bridge, project, () => shown, async () => load());
}

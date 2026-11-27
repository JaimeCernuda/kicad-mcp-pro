// The stackup view (KV14): layer order, materials, thicknesses and net classes.

import { textOf, type Bridge } from "../bridge";
import { button, followRevisions, Frame, h } from "../ui";

interface StackupLayer {
    name: string;
    type: string;
    material: string;
    thickness_mm: number | null;
    epsilon_r: number | null;
    loss_tangent: number | null;
}

interface StackupData {
    revision: number;
    layers: StackupLayer[];
    board_thickness_mm: number | null;
    net_classes: Array<{ name: string; track_width_mm: number | null; clearance_mm: number | null; via_diameter_mm: number | null; via_drill_mm: number | null }>;
}

export async function stackupView(bridge: Bridge, root: HTMLElement, project: Record<string, unknown>): Promise<void> {
    const frame = new Frame(root, "Stackup");
    let data: StackupData | null = null;
    let shown = 0;
    let edits = new Map<number, number>();
    const layersTable = h("table", { class: "kv", "aria-label": "Layers" });
    const classesTable = h("table", { class: "kv", "aria-label": "Net classes" });
    frame.body.append(layersTable, h("div", { class: "kv-group" }, "Net classes"), classesTable);

    const render = () => {
        layersTable.replaceChildren(
            h("thead", {}, h("tr", {}, ...["Layer", "Type", "Material", "Thickness (mm)", "εr", "Loss tangent"].map((label) => h("th", { scope: "col" }, label)))),
        );
        const body = h("tbody");
        (data?.layers ?? []).forEach((layer, index) => {
            const input = h("input", {
                class: "kv",
                type: "number",
                step: "0.001",
                min: "0",
                value: String(edits.get(index) ?? layer.thickness_mm ?? ""),
                style: "width:7em",
                "aria-label": `Thickness of ${layer.name}`,
                // On every keystroke: a "change" event only comes when the field loses focus,
                // so a click on Save right after typing found the button still disabled.
                onInput: (event: Event) => {
                    edits.set(index, Number((event.target as HTMLInputElement).value));
                    save.disabled = false;
                },
            });
            body.append(
                h(
                    "tr",
                    {},
                    h("td", {}, layer.name),
                    h("td", { class: "kv-muted" }, layer.type),
                    h("td", {}, layer.material),
                    h("td", {}, isCopper(layer) || layer.thickness_mm === null ? String(layer.thickness_mm ?? "") : input),
                    h("td", {}, layer.epsilon_r === null ? "" : String(layer.epsilon_r)),
                    h("td", {}, layer.loss_tangent === null ? "" : String(layer.loss_tangent)),
                ),
            );
        });
        layersTable.append(body);
        classesTable.replaceChildren(
            h("thead", {}, h("tr", {}, ...["Net class", "Track width", "Clearance", "Via", "Drill"].map((label) => h("th", { scope: "col" }, label)))),
            h(
                "tbody",
                {},
                ...(data?.net_classes ?? []).map((row) =>
                    h("tr", {}, h("td", {}, row.name), h("td", {}, mm(row.track_width_mm)), h("td", {}, mm(row.clearance_mm)), h("td", {}, mm(row.via_diameter_mm)), h("td", {}, mm(row.via_drill_mm))),
                ),
            ),
        );
    };

    const load = async () => {
        data = await bridge.callData<StackupData>("view_data_stackup", project);
        shown = data.revision;
        edits = new Map();
        save.disabled = true;
        render();
        frame.say(`${data.layers.length} layers${data.board_thickness_mm ? `, ${data.board_thickness_mm} mm board` : ""}, revision ${shown}.`);
    };

    const save = button("Save thicknesses", () =>
        void frame.run("Save the stackup", async () => {
            const layers = (data?.layers ?? []).map((layer, index) => ({
                name: layer.name,
                type: layer.type,
                material: layer.material,
                thickness_mm: edits.get(index) ?? layer.thickness_mm,
                epsilon_r: layer.epsilon_r,
                loss_tangent: layer.loss_tangent,
            }));
            const result = await bridge.callTool("pcb_set_stackup", { layers });
            if (result.isError) throw new Error(textOf(result));
            const gate = await bridge.callTool("pcb_stackup_consistency_gate");
            await load();
            const summary = (gate.structuredContent as any)?.summary ?? textOf(gate).split("\n")[0];
            return `${textOf(result).split("\n")[0]} Consistency: ${summary}`;
        }),
    );
    save.disabled = true;
    const check = button("Check consistency", () =>
        void frame.run("Check the stackup", async () => {
            const gate = await bridge.callTool("pcb_stackup_consistency_gate");
            return (gate.structuredContent as any)?.summary ?? textOf(gate).split("\n")[0];
        }),
    );
    frame.bar.append(save, check);
    frame.say("Reading the stackup…", "busy");
    await load();
    followRevisions(bridge, project, () => shown, async () => load());
}

/** Copper is set by the board's layers, not here: by its type, or a ``*.Cu``/``*_Cu`` name. */
function isCopper(layer: StackupLayer): boolean {
    return ["copper", "signal"].includes(String(layer.type).toLowerCase()) || /[._]Cu$/.test(layer.name);
}

function mm(value: number | null): string {
    return value === null || value === undefined ? "" : `${value} mm`;
}

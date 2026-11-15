// The BOM view (KV12): rows with sourcing and key specs; every operation is a server tool.

import { textOf, textResult, type Bridge } from "../bridge";
import { button, followRevisions, Frame, h, SELECT_SCHEMA, sharedTarget } from "../ui";

export interface SpecCell {
    name: string;
    display: string;
    source: string;
}

interface BomRow {
    references: string[];
    value: string;
    footprint: string;
    mpn: string;
    lcsc: string;
    manufacturer: string;
    dnp: boolean;
    stock: number | null;
    price: number | null;
    currency: string;
    lifecycle: string;
    datasheet: string;
    specs: SpecCell[];
}

interface BomData {
    revision: number;
    quantity: number;
    rows: BomRow[];
    sources: { answered_by: string[]; failed: Array<{ source: string; reason: string }>; could_configure: Array<{ source: string }> };
}

const KEY_SPECS = ["capacitance", "resistance", "inductance", "voltage_rating", "tolerance", "temperature_coefficient", "power_rating", "output_voltage", "output_current"];

export function keySpecs(specs: SpecCell[], limit = 4): string {
    const seen = new Set<string>();
    const shown: string[] = [];
    for (const name of KEY_SPECS) {
        const found = specs.find((spec) => spec.name === name);
        if (found && !seen.has(name)) {
            seen.add(name);
            shown.push(found.display);
        }
        if (shown.length >= limit) break;
    }
    return shown.join(" · ");
}

export async function bomView(bridge: Bridge, root: HTMLElement, project: Record<string, unknown>): Promise<void> {
    const frame = new Frame(root, "Bill of materials");
    let data: BomData | null = null;
    let shown = 0;
    let selected = new Set<string>();
    const quantity = h("input", { class: "kv", type: "number", min: "1", value: String(project.quantity ?? 1), style: "width:6em", "aria-label": "Boards to build" });
    const alternatesBox = h("div");

    const selectedRows = () => (data?.rows ?? []).filter((row) => row.references.some((ref) => selected.has(ref)));
    const reportSelection = () => {
        const rows = selectedRows();
        const refs = rows.flatMap((row) => row.references);
        bridge.report(
            "selection",
            rows.length ? { kind: "bom_row", references: refs, rows: rows.map((row) => ({ value: row.value, mpn: row.mpn, lcsc: row.lcsc })) } : null,
            rows.length ? `Selected in the BOM: ${rows.map((row) => `${row.references.join(",")} ${row.value} ${row.mpn || row.lcsc}`).join("; ")}.` : "Nothing is selected in the BOM.",
        );
    };

    const table = h("table", { class: "kv", "aria-label": "Bill of materials" });
    const render = () => {
        table.replaceChildren(
            h(
                "thead",
                {},
                h(
                    "tr",
                    {},
                    ...["References", "Value", "Key specs", "Footprint", "Part number", "Stock", "Price", "Lifecycle", "Datasheet"].map((label) => h("th", { scope: "col" }, label)),
                ),
            ),
        );
        const body = h("tbody");
        for (const row of data?.rows ?? []) {
            const isSelected = row.references.some((ref) => selected.has(ref));
            const sheet = row.datasheet
                ? h("button", { class: "kv", type: "button", onClick: (event: Event) => { event.stopPropagation(); void bridge.openLink(row.datasheet); } }, "Open")
                : h("span", { class: "kv-muted" }, "none");
            const price = row.price === null ? "" : `${row.price.toFixed(4)} ${row.currency || ""}`.trim();
            body.append(
                h(
                    "tr",
                    {
                        class: "kv-row",
                        "aria-selected": String(isSelected),
                        tabindex: "0",
                        onClick: (event: MouseEvent) => {
                            if (!event.shiftKey && !event.metaKey && !event.ctrlKey) selected = new Set();
                            for (const ref of row.references) selected.add(ref);
                            render();
                            reportSelection();
                            showActions();
                        },
                    },
                    h("td", {}, row.references.join(", ")),
                    h("td", {}, row.value, row.dnp ? h("span", { class: "kv-tag", dataset: { kind: "warning" } }, " not fitted") : null),
                    h("td", {}, keySpecs(row.specs) || h("span", { class: "kv-muted" }, "—")),
                    h("td", { class: "kv-muted" }, row.footprint),
                    h("td", {}, row.mpn || row.lcsc || h("span", { class: "kv-muted" }, "none")),
                    h("td", {}, row.stock === null ? "" : String(row.stock)),
                    h("td", {}, price),
                    h("td", {}, row.lifecycle),
                    h("td", {}, sheet),
                ),
            );
        }
        table.append(body);
    };

    const actions = h("div", { class: "kv-bar", hidden: true });
    const showActions = () => {
        const rows = selectedRows();
        actions.hidden = rows.length !== 1;
        alternatesBox.replaceChildren();
    };
    const one = () => selectedRows()[0];
    actions.append(
        button("Alternates", () => {
            const row = one();
            if (!row) return;
            const code = row.lcsc || row.mpn;
            void frame.run(`Alternates for ${row.references.join(",")}`, async () => {
                if (!code) throw new Error("This row has no part number to search from.");
                const result = await bridge.callTool("lib_find_alternative_parts", { lcsc_code: code });
                const text = textOf(result);
                const codes = [...text.matchAll(/\b(C\d{3,})\b/g)].map((m) => m[1]!).filter((c) => c !== code);
                alternatesBox.replaceChildren(
                    h("div", { class: "kv-group" }, `Alternates for ${row.references.join(", ")}`),
                    h("pre", { style: "white-space:pre-wrap;margin:0;padding:6px 8px" }, text),
                    h("div", { class: "kv-bar" }, ...[...new Set(codes)].slice(0, 8).map((alt) =>
                        button(`Choose ${alt}`, () => void choose(row, alt)),
                    )),
                );
                return `${codes.length} alternates listed.`;
            });
        }),
        button("Not fitted", () => {
            const row = one();
            if (!row) return;
            void frame.run(`Mark ${row.references.join(",")} ${row.dnp ? "fitted" : "not fitted"}`, async () => {
                for (const reference of row.references) {
                    const result = await bridge.callTool("sch_set_dnp", { reference, enabled: !row.dnp });
                    if (result.isError) throw new Error(textOf(result));
                }
                await load();
                return `${row.references.join(", ")} ${row.dnp ? "fitted again" : "marked not fitted"}.`;
            });
        }),
    );

    const choose = async (row: BomRow, code: string) => {
        await frame.run(`Bind ${code} to ${row.references.join(",")}`, async () => {
            for (const reference of row.references) {
                const result = await bridge.callTool("lib_bind_part_to_symbol", { sym_ref: reference, lcsc_code_or_mpn: code, auto_assign_footprint: false });
                if (result.isError) throw new Error(textOf(result));
            }
            await load();
            return `${row.references.join(", ")} now use ${code}.`;
        });
    };

    const load = async () => {
        data = await bridge.callData<BomData>("view_data_bom", { ...project, quantity: Number(quantity.value) || 1 });
        shown = data.revision;
        const known = new Set(data.rows.flatMap((row) => row.references));
        selected = new Set([...selected].filter((ref) => known.has(ref)));
        render();
        showActions();
        const failed = data.sources.failed.map((f) => `${f.source}: ${f.reason}`).join("; ");
        frame.say(`${data.rows.length} rows at ${data.quantity} boards. Sources: ${data.sources.answered_by.join(", ") || "none"}.${failed ? ` Failed: ${failed}.` : ""}`);
        bridge.report("revision", shown, `The BOM view shows revision ${shown}.`);
    };

    frame.bar.append(
        h("label", { class: "kv-filter" }, "Boards", quantity),
        button("Reprice", () => void frame.run("Reprice", async () => { await load(); return `Repriced for ${quantity.value} boards.`; })),
    );
    frame.root.insertBefore(actions, frame.body);
    frame.body.append(table, alternatesBox);

    bridge.tools.set("select_row", {
        name: "select_row",
        description: "Select the BOM rows for these references (R1, C3) so the person sees them.",
        inputSchema: { type: "object", properties: { references: { type: "array", items: { type: "string" } } }, required: ["references"] },
        run: async (args) => {
            selected = new Set((args.references as string[]) ?? []);
            render();
            reportSelection();
            showActions();
            table.querySelector('tr[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
            return textResult(`Selected ${selectedRows().length} BOM rows.`);
        },
    });
    bridge.tools.set("select", {
        name: "select",
        description: "Select the BOM rows of the references another view selected.",
        inputSchema: SELECT_SCHEMA,
        run: async (args) => {
            const refs = sharedTarget(args.selection).references;
            if (!refs.length) return textResult("That selection names no part of the BOM.");
            selected = new Set(refs);
            render();
            showActions();
            table.querySelector('tr[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
            return textResult(`Selected the rows of ${refs.join(", ")}.`);
        },
    });
    bridge.toolsChanged();
    frame.say("Reading the BOM…", "busy");
    await load();
    followRevisions(bridge, project, () => shown, async () => load());
}

// The parts view: a spec table with filters, column choice and side-by-side compare.
// Search is lib_search_parametric, compare lib_compare_parts, pick lib_bind_part_to_symbol.

import { textOf, textResult, type Bridge } from "../bridge";
import { button, Frame, h } from "../ui";
import type { SpecCell } from "./bom";

interface PartRow {
    mpn: string;
    lcsc: string;
    source: string;
    package: string;
    stock: number;
    price: number | null;
    specs: SpecCell[];
}

interface PartsData {
    rows: PartRow[];
    account: { answered_by: string[]; failed: Array<{ source: string; reason: string }>; could_configure: Array<{ source: string; variables: string[]; adds: string }> };
}

interface Filter {
    name: string;
    op: string;
    value: string;
    unit: string;
}

const OPS = ["=", ">=", "<=", ">", "<", "!=", "contains"];

export async function partsView(bridge: Bridge, root: HTMLElement, project: Record<string, unknown>): Promise<void> {
    const frame = new Frame(root, "Parts");
    let data: PartsData | null = null;
    let filters: Filter[] = ((project.filters as Filter[]) ?? []).map((f) => ({ name: f.name, op: f.op ?? "=", value: String(f.value ?? ""), unit: f.unit ?? "" }));
    let columns: string[] = [];
    const picked = new Set<string>();
    let focusRow = "";

    const category = h("input", { class: "kv", value: String(project.category ?? project.query ?? ""), placeholder: "Category, e.g. capacitors", "aria-label": "Category" });
    const pkg = h("input", { class: "kv", value: String(project.package ?? ""), placeholder: "Package", style: "width:7em", "aria-label": "Package" });
    const minStock = h("input", { class: "kv", type: "number", min: "0", value: String(project.min_stock ?? 1), style: "width:6em", "aria-label": "Minimum stock" });
    const symbol = h("input", { class: "kv", value: String(project.symbol ?? ""), placeholder: "Symbol, e.g. U2", style: "width:7em", "aria-label": "Symbol to bind" });
    const filterRow = h("div", { class: "kv-filters", "aria-label": "Filters" });
    const columnBox = h("div", { class: "kv-filters", "aria-label": "Columns" });
    const table = h("table", { class: "kv", "aria-label": "Parts" });
    const compareBox = h("div", { class: "kv-compare" });

    const key = (row: PartRow) => row.lcsc || row.mpn;
    const rowsBy = () => new Map((data?.rows ?? []).map((row) => [key(row), row]));

    const report = () => {
        const rows = rowsBy();
        const chosen = [...picked].map((k) => rows.get(k)).filter(Boolean) as PartRow[];
        const focused = rows.get(focusRow);
        bridge.report(
            "selection",
            focused || chosen.length ? { kind: "part", part: focused ? { mpn: focused.mpn, lcsc: focused.lcsc, source: focused.source } : null, compared: chosen.map((row) => ({ mpn: row.mpn, lcsc: row.lcsc })) } : null,
            `${focused ? `Selected part: ${focused.mpn} (${focused.lcsc || focused.source}). ` : ""}${chosen.length ? `Compared: ${chosen.map((row) => row.mpn).join(", ")}.` : ""}` || "No part is selected.",
        );
    };

    const renderFilters = () => {
        filterRow.replaceChildren(
            ...filters.map((filter, index) => {
                const name = h("input", { class: "kv", value: filter.name, style: "width:9em", "aria-label": "Spec name", onChange: (e: Event) => (filter.name = (e.target as HTMLInputElement).value) });
                const op = h("select", { class: "kv", "aria-label": "Comparison", onChange: (e: Event) => (filter.op = (e.target as HTMLSelectElement).value) }, ...OPS.map((o) => h("option", { value: o, selected: o === filter.op }, o)));
                const value = h("input", { class: "kv", value: filter.value, style: "width:6em", "aria-label": "Value", onChange: (e: Event) => (filter.value = (e.target as HTMLInputElement).value) });
                const unit = h("input", { class: "kv", value: filter.unit, style: "width:4em", placeholder: "unit", "aria-label": "Unit", onChange: (e: Event) => (filter.unit = (e.target as HTMLInputElement).value) });
                return h("span", { class: "kv-filter" }, name, op, value, unit, button("×", () => { filters.splice(index, 1); renderFilters(); }, "Remove this filter"));
            }),
            button("Add filter", () => { filters.push({ name: "", op: ">=", value: "", unit: "" }); renderFilters(); }),
        );
    };

    const allSpecNames = () => {
        const names = new Map<string, number>();
        for (const row of data?.rows ?? []) for (const spec of row.specs) names.set(spec.name, (names.get(spec.name) ?? 0) + 1);
        return [...names.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => name).filter((name) => name !== "package");
    };

    const renderColumns = () => {
        const names = allSpecNames();
        if (!columns.length) columns = [...new Set([...filters.map((f) => f.name).filter(Boolean), ...names])].slice(0, 5);
        columnBox.replaceChildren(
            h("span", { class: "kv-muted" }, "Columns:"),
            ...names.slice(0, 24).map((name) =>
                h("label", { class: "kv-filter" }, h("input", { type: "checkbox", checked: columns.includes(name), onChange: (e: Event) => {
                    columns = (e.target as HTMLInputElement).checked ? [...columns, name] : columns.filter((c) => c !== name);
                    renderTable();
                } }), name),
            ),
        );
    };

    const value = (row: PartRow, name: string) => row.specs.filter((spec) => spec.name === name).map((spec) => spec.display)[0] ?? "";

    const renderTable = () => {
        table.replaceChildren(
            h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Compare"), ...["Part", "Source", "Package", "Stock", "Price", ...columns].map((label) => h("th", { scope: "col" }, label)))),
        );
        const body = h("tbody");
        for (const row of data?.rows ?? []) {
            const k = key(row);
            body.append(
                h(
                    "tr",
                    { class: "kv-row", tabindex: "0", "aria-selected": String(focusRow === k), onClick: () => { focusRow = k; renderTable(); report(); } },
                    h("td", {}, h("input", { type: "checkbox", "aria-label": `Compare ${row.mpn}`, checked: picked.has(k), onClick: (e: Event) => e.stopPropagation(), onChange: (e: Event) => {
                        if ((e.target as HTMLInputElement).checked) picked.add(k); else picked.delete(k);
                        report();
                    } })),
                    h("td", {}, row.mpn, row.lcsc ? h("div", { class: "kv-muted" }, row.lcsc) : null),
                    h("td", { class: "kv-muted" }, row.source),
                    h("td", {}, row.package),
                    h("td", {}, String(row.stock)),
                    h("td", {}, row.price === null ? "" : row.price.toFixed(4)),
                    ...columns.map((name) => h("td", {}, value(row, name))),
                ),
            );
        }
        table.append(body);
    };

    const search = async () => {
        const args: Record<string, unknown> = {
            category: category.value.trim(),
            filters: filters.filter((f) => f.name && f.value).map((f) => ({ name: f.name, op: f.op, value: f.value, unit: f.unit })),
            package: pkg.value.trim(),
            min_stock: Number(minStock.value) || 0,
        };
        if (Array.isArray(project.candidates) && project.candidates.length && !data) args.candidates = project.candidates;
        data = await bridge.callData<PartsData>("view_data_parts", args);
        columns = [];
        renderColumns();
        renderTable();
        const extra = data.account.could_configure.length
            ? ` Not configured: ${data.account.could_configure.map((row) => `${row.source} (${row.variables.join(", ")})`).join(", ")}.`
            : "";
        const failed = data.account.failed.map((row) => `${row.source}: ${row.reason}`).join("; ");
        return `${data.rows.length} parts from ${data.account.answered_by.join(", ") || "no source"}.${failed ? ` Failed: ${failed}.` : ""}${extra}`;
    };

    const compare = async () => {
        const parts = [...picked];
        if (parts.length < 2) throw new Error("Tick two or more parts to compare.");
        const result = await bridge.callData<{ parts: string[]; rows: Array<Record<string, unknown>> }>("view_data_compare", { parts, category: category.value.trim() });
        compareBox.replaceChildren(
            h("div", { class: "kv-group" }, `Comparing ${result.parts.join(", ")}`),
            h(
                "table",
                { class: "kv" },
                h("thead", {}, h("tr", {}, h("th", {}, "Attribute"), ...result.parts.map((part) => h("th", {}, part)))),
                h("tbody", {}, ...result.rows.map((row) => h("tr", {}, h("td", {}, String(row.attribute)), ...result.parts.map((part) => h("td", {}, ((row[part] as Array<{ display: string; source: string }>) ?? []).map((cell) => `${cell.display} [${cell.source}]`).join("; ")))))),
            ),
        );
        return `Compared ${result.parts.length} parts.`;
    };

    const pick = async () => {
        const row = rowsBy().get(focusRow);
        if (!row) throw new Error("Select the part to use first.");
        const target = symbol.value.trim();
        if (!target) throw new Error("Name the symbol to bind it to, e.g. U2.");
        const result = await bridge.callTool("lib_bind_part_to_symbol", { sym_ref: target, lcsc_code_or_mpn: row.lcsc || row.mpn, source: row.source.split(":")[0] });
        if (result.isError) throw new Error(textOf(result));
        bridge.report("picked", { symbol: target, part: { mpn: row.mpn, lcsc: row.lcsc } }, `The person bound ${row.mpn} to ${target}.`);
        return textOf(result).split("\n")[0] || `Bound ${row.mpn} to ${target}.`;
    };

    frame.bar.append(
        category,
        pkg,
        h("label", { class: "kv-filter" }, "Min stock", minStock),
        button("Search", () => void frame.run("Search", search)),
        h("span", { class: "kv-spacer" }),
        button("Compare", () => void frame.run("Compare", compare)),
        symbol,
        button("Use for symbol", () => void frame.run("Bind the part", pick)),
    );
    frame.root.insertBefore(filterRow, frame.body);
    frame.root.insertBefore(columnBox, frame.body);
    frame.body.append(table, compareBox);
    renderFilters();

    bridge.tools.set("select_row", {
        name: "select_row",
        description: "Select a part row (by MPN or LCSC code) and optionally tick parts for comparison.",
        inputSchema: { type: "object", properties: { part: { type: "string" }, compare: { type: "array", items: { type: "string" } } } },
        run: async (args) => {
            const rows = rowsBy();
            const find = (code: string) => [...rows.keys()].find((k) => k === code || rows.get(k)?.mpn === code);
            if (typeof args.part === "string") focusRow = find(args.part) ?? focusRow;
            if (Array.isArray(args.compare)) for (const code of args.compare as string[]) { const k = find(code); if (k) picked.add(k); }
            renderTable();
            report();
            return textResult("Selected.");
        },
    });
    bridge.toolsChanged();
    if (category.value || (Array.isArray(project.candidates) && project.candidates.length)) {
        await frame.run("Search", search);
        if (Array.isArray(project.candidates)) for (const row of (data as PartsData | null)?.rows ?? []) picked.add(key(row));
        renderTable();
    } else {
        frame.say("Name a category and add filters, then Search.");
    }
}

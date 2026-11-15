// The checks view (KV13): DRC and ERC violations grouped by rule; selecting one reports it.

import { textOf, textResult, type Bridge } from "../bridge";
import { button, Frame, h } from "../ui";

interface Violation {
    kind: "drc" | "erc";
    rule: string;
    severity: string;
    description: string;
    items: Array<{ description: string; uuid?: string; position_mm?: { x: number; y: number } }>;
}

interface ChecksData {
    revision: number;
    ran: string[];
    violations: Violation[];
    unconnected: number;
    summaries: Record<string, string>;
}

export async function checksView(bridge: Bridge, root: HTMLElement, project: Record<string, unknown>): Promise<void> {
    const frame = new Frame(root, "Checks");
    const kind = String(project.kind ?? "both");
    let data: ChecksData | null = null;
    let selected = -1;
    const list = h("div", { role: "list" });
    frame.body.append(list);

    const render = () => {
        list.replaceChildren();
        if (!data || !data.violations.length) {
            list.append(h("div", { class: "kv-empty" }, data ? "No violations." : ""));
            return;
        }
        const groups = new Map<string, Array<[Violation, number]>>();
        data.violations.forEach((violation, index) => {
            const key = `${violation.kind.toUpperCase()} · ${violation.rule}`;
            groups.set(key, [...(groups.get(key) ?? []), [violation, index]]);
        });
        for (const [key, rows] of groups) {
            list.append(h("div", { class: "kv-group" }, `${key} (${rows.length})`));
            const table = h("table", { class: "kv" });
            for (const [violation, index] of rows) {
                const where = violation.items.find((item) => item.position_mm)?.position_mm;
                table.append(
                    h(
                        "tr",
                        {
                            class: "kv-row",
                            tabindex: "0",
                            "aria-selected": String(index === selected),
                            onClick: () => choose(index),
                        },
                        h("td", { style: "width:6em" }, h("span", { class: "kv-tag", dataset: { kind: violation.severity === "error" ? "error" : "warning" } }, violation.severity)),
                        h("td", {}, violation.description, h("div", { class: "kv-muted" }, violation.items.map((item) => item.description).join(" · "))),
                        h("td", { style: "width:9em" }, where ? `(${where.x}, ${where.y}) mm` : ""),
                    ),
                );
            }
            list.append(table);
        }
    };

    const choose = (index: number) => {
        selected = index;
        render();
        const violation = data?.violations[index];
        if (!violation) return;
        const where = violation.items.find((item) => item.position_mm)?.position_mm;
        bridge.report(
            "selection",
            { kind: "violation", check: violation.kind, rule: violation.rule, severity: violation.severity, description: violation.description, position_mm: where, items: violation.items },
            `Selected in the checks: ${violation.kind.toUpperCase()} ${violation.severity} ${violation.rule}: ${violation.description}${where ? ` at (${where.x}, ${where.y}) mm` : ""}.`,
        );
    };

    const load = async (run: boolean) => {
        data = await bridge.callData<ChecksData>("view_data_checks", { ...project, kind, run });
        selected = -1;
        render();
        bridge.report("revision", data.revision, `The checks view ran against revision ${data.revision}.`);
        const parts = Object.entries(data.summaries).map(([name, text]) => `${name.toUpperCase()}: ${text}`);
        return parts.join(" ") || `${data.violations.length} violations.`;
    };

    frame.bar.append(
        button("Run again", () => void frame.run("Run the checks", async () => load(true))),
    );
    bridge.tools.set("select_row", {
        name: "select_row",
        description: "Select the violation at this index in the list (0 is the first).",
        inputSchema: { type: "object", properties: { index: { type: "number" } }, required: ["index"] },
        run: async (args) => {
            choose(Number(args.index));
            return textResult("Selected.");
        },
    });
    bridge.toolsChanged();
    await frame.run("Run the checks", async () => load(false));
    void textOf;
}

// What lies under a rectangle of a view (describe_region, KF2).

export interface Rect {
    x: number;
    y: number;
    width: number;
    height: number;
}

interface WorldBox {
    x1: number;
    y1: number;
    x2: number;
    y2: number;
}

function worldBox(viewer: any, rect: Rect, root: DOMRect): WorldBox {
    const a = viewer.screenToWorld(root.left + rect.x, root.top + rect.y);
    const b = viewer.screenToWorld(root.left + rect.x + rect.width, root.top + rect.y + rect.height);
    return { x1: Math.min(a.x, b.x), y1: Math.min(a.y, b.y), x2: Math.max(a.x, b.x), y2: Math.max(a.y, b.y) };
}

function overlaps(box: any, area: WorldBox): boolean {
    if (!box) return false;
    const x1 = box.x;
    const y1 = box.y;
    const x2 = box.x2 ?? box.x + box.w;
    const y2 = box.y2 ?? box.y + box.h;
    return x1 <= area.x2 && x2 >= area.x1 && y1 <= area.y2 && y2 >= area.y1;
}

function inside(point: any, area: WorldBox): boolean {
    return Boolean(point) && point.x >= area.x1 && point.x <= area.x2 && point.y >= area.y1 && point.y <= area.y2;
}

const r = (value: number) => Math.round(value * 1000) / 1000;

export function regionOnBoard(viewer: any, rect: Rect, root: DOMRect) {
    if (!viewer?.board) throw new Error("The board is not drawn yet.");
    const area = worldBox(viewer, rect, root);
    const board = viewer.board;
    const netName = (number: unknown) => (typeof number === "number" ? board.getNetName?.(number) : undefined);
    const nets = new Set<string>();
    const footprints: Array<Record<string, unknown>> = [];
    const pads: Array<Record<string, unknown>> = [];
    for (const fp of board.footprints ?? []) {
        if (!overlaps(fp.bbox, area)) continue;
        const at = fp.at?.position;
        footprints.push({
            reference: fp.reference,
            value: fp.value,
            layer: fp.layer,
            position_mm: at ? { x: r(at.x), y: r(at.y) } : undefined,
            locked: Boolean(fp.locked),
        });
        for (const pad of fp.pads ?? []) {
            if (pad.bbox && !overlaps(pad.bbox, area)) continue;
            const name = pad.net?.name ?? netName(pad.net?.number);
            if (name) nets.add(name);
            pads.push({ reference: `${fp.reference}.${pad.number}`, net: name });
        }
    }
    const tracks: Array<Record<string, unknown>> = [];
    for (const segment of board.segments ?? []) {
        if (!(inside(segment.start, area) || inside(segment.end, area) || overlaps(segment.bbox, area))) continue;
        const name = netName(segment.net);
        if (name) nets.add(name);
        tracks.push({
            net: name,
            layer: segment.layer,
            width_mm: segment.width,
            start_mm: segment.start ? { x: r(segment.start.x), y: r(segment.start.y) } : undefined,
            end_mm: segment.end ? { x: r(segment.end.x), y: r(segment.end.y) } : undefined,
        });
    }
    const vias: Array<Record<string, unknown>> = [];
    for (const via of board.vias ?? []) {
        const at = via.at?.position;
        if (!inside(at, area)) continue;
        const name = netName(via.net);
        if (name) nets.add(name);
        vias.push({ net: name, position_mm: { x: r(at.x), y: r(at.y) } });
    }
    return {
        view: "board",
        region_mm: { x1: r(area.x1), y1: r(area.y1), x2: r(area.x2), y2: r(area.y2) },
        footprints,
        pads: pads.slice(0, 200),
        tracks: tracks.slice(0, 200),
        vias: vias.slice(0, 200),
        nets: [...nets].sort(),
    };
}

export function regionOnSchematic(viewer: any, rect: Rect, root: DOMRect) {
    const sheet = viewer?.schematic ?? viewer?.document;
    if (!sheet) throw new Error("The schematic is not drawn yet.");
    const area = worldBox(viewer, rect, root);
    const symbols: Array<Record<string, unknown>> = [];
    const nets = new Set<string>();
    const all = sheet.symbols instanceof Map ? [...sheet.symbols.values()] : sheet.symbols ?? [];
    for (const symbol of all) {
        const at = symbol.at?.position;
        if (!inside(at, area) && !overlaps(symbol.bbox, area)) continue;
        const pins: Array<Record<string, unknown>> = [];
        for (const pin of symbol.unit_pins ?? symbol.pins ?? []) {
            pins.push({ number: pin.number ?? pin.definition?.number?.text, name: pin.definition?.name?.text });
        }
        symbols.push({ reference: symbol.reference ?? symbol.default_instance?.reference ?? symbol.properties?.get?.("Reference")?.text, value: symbol.value ?? symbol.default_instance?.value, position: at ? { x: r(at.x), y: r(at.y) } : undefined, pins: pins.slice(0, 64) });
    }
    for (const label of [...(sheet.net_labels ?? []), ...(sheet.global_labels ?? []), ...(sheet.hierarchical_labels ?? [])]) {
        if (inside(label.at?.position, area) && label.text) nets.add(label.text);
    }
    return {
        view: "schematic",
        sheet: sheet.filename,
        region: { x1: r(area.x1), y1: r(area.y1), x2: r(area.x2), y2: r(area.y2) },
        symbols,
        nets: [...nets].sort(),
    };
}

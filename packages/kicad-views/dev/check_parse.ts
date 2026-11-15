// Every board and schematic of the fixture corpus and dev/samples through ecad-viewer's own
// parser and documents; a board's nets must resolve: each net the file names is on the
// board, and each pad that names a net has it. Run: npm run check:parse
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { BoardParser, SchematicParser } from "kicad-parser";
import { KicadPCB } from "ecad-viewer-app/src/kicad/board";
import { KicadSch } from "ecad-viewer-app/src/kicad/schematic";

// Damaged on purpose: the server refuses them before a view opens (instance/board_file.py).
const DAMAGED = new Set(["malformed-pcb.kicad_pcb", "malformed-sch.kicad_sch"]);

function files(dir: string, out: string[] = []): string[] {
    for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) files(path, out);
        else if (/\.kicad_(pcb|sch)$/.test(name) && !DAMAGED.has(name)) out.push(path);
    }
    return out;
}

const failures: string[] = [];
const all = process.argv.slice(2).flatMap((dir) => files(dir));
for (const path of all) {
    try {
        const text = readFileSync(path, "utf8");
        if (path.endsWith(".kicad_sch")) {
            new KicadSch(path, new SchematicParser().parse(text) as never);
            continue;
        }
        const board = new KicadPCB(path, new BoardParser().parse(text) as never);
        const named = new Set<string>();
        for (const m of text.matchAll(/\(net (?:\d+ )?"((?:\\.|[^"\\])*)"\)/g)) if (m[1]) named.add(m[1]);
        const resolved = new Set(board.nets.map((net) => net.name).filter(Boolean));
        const missing = [...named].filter((name) => !resolved.has(name));
        const pads = board.footprints.flatMap((fp) => fp.pads).filter((pad) => pad.net?.name);
        const unresolved = pads.filter((pad) => !(pad.net!.number > 0 && board.getNetName(pad.net!.number) === pad.net!.name));
        if (missing.length || unresolved.length) {
            failures.push(`${path}: nets not on the board ${missing.slice(0, 5).join(", ")}; pads unresolved ${unresolved.length}/${pads.length}`);
        }
    } catch (error) {
        failures.push(`${path}: ${error instanceof Error ? error.message : String(error)}`);
    }
}
console.log(`${all.length - failures.length}/${all.length} files parsed with their nets.`);
for (const failure of failures) console.log(failure);
process.exit(failures.length ? 1 : 0);

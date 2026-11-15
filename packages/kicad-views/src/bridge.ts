// The view's side of the MCP Apps protocol (2026-01-26) and the draft's app tools.
//
// Requests to the host: ui/initialize, tools/call (server tools), resources/read,
// ui/update-model-context, ui/open-link, ui/request-display-mode, ui/message.
// Requests from the host: tools/list and tools/call (this view's own tools),
// ui/resource-teardown. Notifications from the host: tool-input, tool-result,
// host-context-changed.

export const PROTOCOL = "2026-01-26";

/** How long a view waits for one data call before it says so. */
export const DATA_TIMEOUT_MS = 120_000;

export interface ToolContent {
    type: string;
    text?: string;
    data?: string;
    mimeType?: string;
}

export interface ToolResult {
    content?: ToolContent[];
    structuredContent?: Record<string, unknown>;
    isError?: boolean;
}

export interface AppTool {
    name: string;
    description: string;
    inputSchema: Record<string, unknown>;
    run: (args: Record<string, unknown>) => Promise<ToolResult>;
}

type Pending = { resolve: (value: unknown) => void; reject: (reason: unknown) => void };

export class Bridge {
    hostContext: Record<string, unknown> = {};
    toolInput: Record<string, unknown> = {};
    toolResult: ToolResult | null = null;
    readonly tools = new Map<string, AppTool>();
    #next = 1;
    #pending = new Map<number | string, Pending>();
    #listeners = new Map<string, Array<(params: Record<string, unknown>) => void>>();
    #contextTimers = new Map<string, number>();
    #contextLast = new Map<string, number>();
    #context: Record<string, unknown> = {};
    #contextText = new Map<string, string>();

    constructor() {
        window.addEventListener("message", (event) => this.#onMessage(event));
    }

    on(method: string, listener: (params: Record<string, unknown>) => void): void {
        const list = this.#listeners.get(method) ?? [];
        list.push(listener);
        this.#listeners.set(method, list);
    }

    #send(message: Record<string, unknown>): void {
        window.parent.postMessage({ jsonrpc: "2.0", ...message }, "*");
    }

    /** Send a request to the host; with ``timeoutMs``, give up (and say so) after that long. */
    request<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs = 0): Promise<T> {
        const id = this.#next++;
        this.#send({ id, method, params });
        return new Promise<T>((resolve, reject) => {
            let timer = 0;
            const settle = (done: () => void) => {
                if (timer) window.clearTimeout(timer);
                done();
            };
            this.#pending.set(id, {
                resolve: (value) => settle(() => resolve(value as T)),
                reject: (reason) => settle(() => reject(reason)),
            });
            if (timeoutMs > 0) {
                timer = window.setTimeout(() => {
                    this.#pending.delete(id);
                    const what = typeof params.name === "string" ? params.name : method;
                    reject(new Error(`No answer from ${what} after ${Math.round(timeoutMs / 1000)} s.`));
                }, timeoutMs);
            }
        });
    }

    notify(method: string, params: Record<string, unknown> = {}): void {
        this.#send({ method, params });
    }

    /** Call a server tool through the host. */
    async callTool(name: string, args: Record<string, unknown> = {}, timeoutMs = 0): Promise<ToolResult> {
        return this.request<ToolResult>("tools/call", { name, arguments: args }, timeoutMs);
    }

    /**
     * Call a server tool and return its structured content, or throw with its text. A
     * view's data call that gets no answer within ``timeoutMs`` fails with a message
     * instead of leaving the view waiting.
     */
    async callData<T = Record<string, unknown>>(
        name: string,
        args: Record<string, unknown> = {},
        timeoutMs = DATA_TIMEOUT_MS,
    ): Promise<T> {
        const result = await this.callTool(name, args, timeoutMs);
        const text = textOf(result);
        if (result.isError) throw new Error(text || `${name} failed`);
        if (result.structuredContent && typeof result.structuredContent === "object") {
            const structured = result.structuredContent as Record<string, unknown>;
            return (("result" in structured ? structured.result : structured) as T);
        }
        try {
            return JSON.parse(text) as T;
        } catch {
            throw new Error(text || `${name} returned no data`);
        }
    }

    /**
     * Report one key of the view's context to the agent (MA8), at most ten times a
     * second per key; the last value wins.
     */
    report(key: string, value: unknown, text: string): void {
        this.#context[key] = value;
        this.#contextText.set(key, text);
        if (this.#contextTimers.has(key)) return;
        const wait = Math.max(0, 100 - (Date.now() - (this.#contextLast.get(key) ?? 0)));
        this.#contextTimers.set(
            key,
            window.setTimeout(() => {
                this.#contextTimers.delete(key);
                this.#contextLast.set(key, Date.now());
                const lines = [...this.#contextText.values()].filter(Boolean);
                void this.request("ui/update-model-context", {
                    content: [{ type: "text", text: lines.join("\n") }],
                    structuredContent: { ...this.#context },
                }).catch(() => undefined);
            }, wait),
        );
    }

    async openLink(url: string): Promise<void> {
        await this.request("ui/open-link", { url });
    }

    async initialize(name: string, version: string): Promise<void> {
        const result = await this.request<Record<string, unknown>>("ui/initialize", {
            appInfo: { name, version },
            appCapabilities: {
                availableDisplayModes: ["inline", "fullscreen"],
                tools: { listChanged: true },
            },
            protocolVersion: PROTOCOL,
        });
        this.hostContext = (result?.hostContext as Record<string, unknown>) ?? {};
        applyTheme(this.hostContext);
        this.notify("ui/notifications/initialized");
    }

    async requestFullscreen(): Promise<void> {
        const modes = (this.hostContext.availableDisplayModes as string[] | undefined) ?? [];
        if (modes.includes("fullscreen") && this.hostContext.displayMode !== "fullscreen") {
            await this.request("ui/request-display-mode", { mode: "fullscreen" }).catch(() => undefined);
        }
    }

    toolsChanged(): void {
        this.notify("notifications/tools/list_changed");
    }

    #emit(method: string, params: Record<string, unknown>): void {
        for (const listener of this.#listeners.get(method) ?? []) listener(params);
    }

    async #answer(id: number | string, method: string, params: Record<string, unknown>): Promise<void> {
        try {
            if (method === "tools/list") {
                const tools = [...this.tools.values()].map(({ name, description, inputSchema }) => ({
                    name,
                    description,
                    inputSchema,
                }));
                this.#send({ id, result: { tools } });
                return;
            }
            if (method === "tools/call") {
                const tool = this.tools.get(String(params.name ?? ""));
                if (!tool) {
                    this.#send({ id, error: { code: -32602, message: `No view tool ${String(params.name)}` } });
                    return;
                }
                const args = (params.arguments as Record<string, unknown>) ?? {};
                let result: ToolResult;
                try {
                    result = await tool.run(args);
                } catch (error) {
                    result = { isError: true, content: [{ type: "text", text: messageOf(error) }] };
                }
                this.#send({ id, result });
                return;
            }
            if (method === "ui/resource-teardown") {
                this.#emit(method, params);
                this.#send({ id, result: {} });
                return;
            }
            this.#send({ id, error: { code: -32601, message: "Method not found" } });
        } catch (error) {
            this.#send({ id, error: { code: -32603, message: messageOf(error) } });
        }
    }

    #onMessage(event: MessageEvent): void {
        const data = event.data as Record<string, unknown> | null;
        if (!data || typeof data !== "object" || data.jsonrpc !== "2.0") return;
        if (event.source !== window.parent) return;
        const id = data.id as number | string | undefined;
        const method = data.method as string | undefined;
        if (id !== undefined && !method) {
            const entry = this.#pending.get(id);
            if (!entry) return;
            this.#pending.delete(id);
            if (data.error) entry.reject(data.error);
            else entry.resolve(data.result);
            return;
        }
        const params = (data.params as Record<string, unknown>) ?? {};
        if (id !== undefined && method) {
            void this.#answer(id, method, params);
            return;
        }
        if (method === "ui/notifications/tool-input") {
            this.toolInput = (params.arguments as Record<string, unknown>) ?? {};
        } else if (method === "ui/notifications/tool-result") {
            this.toolResult = params as ToolResult;
        } else if (method === "ui/notifications/host-context-changed") {
            this.hostContext = { ...this.hostContext, ...params };
            applyTheme(this.hostContext);
        }
        if (method) this.#emit(method, params);
    }
}

export function textOf(result: ToolResult | null | undefined): string {
    return (result?.content ?? [])
        .map((item) => item.text ?? "")
        .filter(Boolean)
        .join("\n");
}

export function messageOf(error: unknown): string {
    if (error instanceof Error) return error.message;
    if (error && typeof error === "object" && "message" in error) return String((error as { message: unknown }).message);
    return String(error);
}

export function textResult(text: string, structured?: Record<string, unknown>): ToolResult {
    return { content: [{ type: "text", text }], ...(structured ? { structuredContent: structured } : {}) };
}

let appliedVariables: string[] = [];

/** Follow the host's theme and style variables (light or dark). */
export function applyTheme(context: Record<string, unknown>): void {
    const root = document.documentElement;
    const theme = context.theme === "dark" || context.theme === "light" ? context.theme : null;
    if (theme) root.dataset.theme = theme;
    const styles = (context.styles as Record<string, unknown> | undefined) ?? {};
    const variables = (styles.variables as Record<string, string> | undefined) ?? {};
    for (const name of appliedVariables) if (!(name in variables)) root.style.removeProperty(name);
    appliedVariables = [];
    for (const [name, value] of Object.entries(variables)) {
        if (/^--[A-Za-z0-9-]+$/.test(name) && typeof value === "string") {
            root.style.setProperty(name, value);
            appliedVariables.push(name);
        }
    }
}

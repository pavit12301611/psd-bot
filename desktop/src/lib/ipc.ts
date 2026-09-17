/**
 * IPC bridge – the ONLY module that talks to the Python backend.
 *
 * Inside the Tauri shell every call goes through `invoke()` to the Rust
 * side, which owns the private sidecar connection; the WebView never sees
 * a URL. When the UI is opened outside Tauri (plain `vite dev` for UI work)
 * we fall back to same-origin fetch through Vite's proxy so the components
 * stay testable.
 */
import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export interface FilePart {
  field: string;
  name: string;
  mime?: string;
  data: string; // base64
}

export interface ApiRequest {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  json?: unknown;
  form?: Record<string, string>;
  files?: FilePart[];
  headers?: Record<string, string>;
}

export interface ApiResponse<T = any> {
  status: number;
  headers: Record<string, string>;
  json: T | null;
  text: string;
  base64?: string | null;
}

export const inTauri = (() => {
  try {
    return isTauri();
  } catch {
    return false;
  }
})();

export class ApiError extends Error {
  status: number;
  body: any;
  constructor(status: number, message: string, body?: any) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function errorMessage(res: ApiResponse): string {
  const j = res.json as any;
  if (j && typeof j === "object") {
    if (typeof j.detail === "string") return j.detail;
    if (typeof j.error === "string") return j.error;
    if (typeof j.message === "string") return j.message;
    if (Array.isArray(j.detail)) return j.detail.map((d: any) => d.msg || JSON.stringify(d)).join(", ");
  }
  return res.text?.slice(0, 200) || `Request failed (${res.status})`;
}

let unauthorizedHandler: (() => void) | null = null;
export function onUnauthorized(fn: () => void) {
  unauthorizedHandler = fn;
}

const REQUEST_TIMEOUT_MS = 60_000;

async function browserFallback(req: ApiRequest): Promise<ApiResponse> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  const init: RequestInit = {
    method: req.method,
    headers: { ...(req.headers || {}) },
    credentials: "same-origin",
    signal: ctrl.signal,
  };
  if (req.files || req.form) {
    const fd = new FormData();
    for (const [k, v] of Object.entries(req.form || {})) fd.append(k, v);
    for (const f of req.files || []) {
      const bin = atob(f.data);
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      fd.append(f.field, new Blob([arr], { type: f.mime || "application/octet-stream" }), f.name);
    }
    init.body = fd;
  } else if (req.json !== undefined) {
    (init.headers as any)["Content-Type"] = "application/json";
    init.body = JSON.stringify(req.json);
  }
  try {
    const r = await fetch(req.path, init);
    const headers: Record<string, string> = {};
    r.headers.forEach((v, k) => (headers[k] = v));
    const ctype = headers["content-type"] || "";
    if (ctype.startsWith("text/") || ctype.includes("json") || ctype === "") {
      const text = await r.text();
      let json: any = null;
      try {
        json = JSON.parse(text);
      } catch {
        /* not json */
      }
      return { status: r.status, headers, json, text };
    }
    const buf = new Uint8Array(await r.arrayBuffer());
    let s = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < buf.length; i += CHUNK) s += String.fromCharCode(...buf.subarray(i, i + CHUNK));
    return { status: r.status, headers, json: null, text: "", base64: btoa(s) };
  } catch (e: any) {
    if (e?.name === "AbortError") return { status: 0, headers: {}, json: { error: "Request timed out" }, text: "Request timed out" };
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/** Low level request. Never throws on HTTP status – check `status`. */
export async function request<T = any>(req: ApiRequest): Promise<ApiResponse<T>> {
  if (inTauri) return invoke<ApiResponse<T>>("api_request", { req });
  return browserFallback(req);
}

/** Convenience: throws ApiError on >= 400 and returns parsed JSON. */
export async function api<T = any>(req: ApiRequest): Promise<T> {
  const res = await request<T>(req);
  if (res.status === 401) {
    unauthorizedHandler?.();
  }
  if (res.status >= 400) throw new ApiError(res.status, errorMessage(res), res.json);
  if (res.status === 0) throw new ApiError(0, errorMessage(res) || "Network error", res.json);
  return (res.json ?? (res.text as unknown)) as T;
}

export const get = <T = any>(path: string) => api<T>({ method: "GET", path });
export const postJson = <T = any>(path: string, json: unknown) => api<T>({ method: "POST", path, json });
export const putJson = <T = any>(path: string, json: unknown) => api<T>({ method: "PUT", path, json });
export const patchJson = <T = any>(path: string, json: unknown) => api<T>({ method: "PATCH", path, json });
export const postForm = <T = any>(path: string, form: Record<string, string>, files?: FilePart[]) =>
  api<T>({ method: "POST", path, form, files });
export const patchForm = <T = any>(path: string, form: Record<string, string>) => api<T>({ method: "PATCH", path, form });
export const del = <T = any>(path: string) => api<T>({ method: "DELETE", path });

/** Build a query string, skipping empty values. */
export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

// ------------------------------------------------------------------
// Streaming (SSE) – forwarded as Tauri events by the Rust side.
// ------------------------------------------------------------------

export interface StreamHandlers {
  onOpen?: (info: { status: number; runId: string }) => void;
  onData: (data: string) => void;
  onError?: (message: string, status: number) => void;
  onDone?: () => void;
}

let streamSeq = 0;

export interface StreamHandle {
  cancel: () => void;
}

export function stream(req: ApiRequest, h: StreamHandlers): StreamHandle {
  const id = `${Date.now().toString(36)}-${++streamSeq}`;
  let cancelled = false;

  if (inTauri) {
    let unlisten: UnlistenFn | null = null;
    const ready = listen<{ kind: string; data: string; status: number; run_id: string }>(
      `api-stream-${id}`,
      (ev) => {
        if (cancelled) return;
        const p = ev.payload;
        if (p.kind === "open") h.onOpen?.({ status: p.status, runId: p.run_id });
        else if (p.kind === "data") h.onData(p.data);
        else if (p.kind === "error") h.onError?.(p.data, p.status);
        else if (p.kind === "done") {
          h.onDone?.();
          unlisten?.();
        }
      },
    );
    ready.then((u) => {
      unlisten = u;
      invoke("api_stream", { id, req }).catch((e) => {
        if (!cancelled) h.onError?.(String(e), 0);
      });
    });
    return {
      cancel: () => {
        cancelled = true;
        unlisten?.();
        invoke("api_stream_cancel", { id }).catch(() => {});
      },
    };
  }

  // Browser fallback: fetch + ReadableStream SSE parsing.
  const ctrl = new AbortController();
  (async () => {
    try {
      const init: RequestInit = { method: req.method, signal: ctrl.signal, headers: { ...(req.headers || {}) } };
      if (req.form) {
        const fd = new FormData();
        for (const [k, v] of Object.entries(req.form)) fd.append(k, v);
        init.body = fd;
      } else if (req.json !== undefined) {
        (init.headers as any)["Content-Type"] = "application/json";
        init.body = JSON.stringify(req.json);
      }
      const r = await fetch(req.path, init);
      h.onOpen?.({ status: r.status, runId: r.headers.get("X-psd.ai-Run-Id") || "" });
      if (r.status >= 400) {
        h.onError?.(await r.text(), r.status);
        return;
      }
      const reader = r.body!.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let evName = "";
          const dataLines: string[] = [];
          for (const line of frame.split("\n")) {
            if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
            else if (line.startsWith("event:")) evName = line.slice(6).trim();
          }
          if (!dataLines.length) continue;
          const data = dataLines.join("\n");
          if (evName === "error") h.onError?.(data, r.status);
          else h.onData(data);
        }
      }
      h.onDone?.();
    } catch (e: any) {
      if (!cancelled) h.onError?.(e?.message || String(e), 0);
    }
  })();
  return { cancel: () => { cancelled = true; ctrl.abort(); } };
}

// ------------------------------------------------------------------
// Backend lifecycle (Tauri only)
// ------------------------------------------------------------------

export type BackendStatus = "starting" | "ready" | "error" | "stopping";

export async function backendStatus(): Promise<{ status: BackendStatus; log: string[] }> {
  if (!inTauri) return { status: "ready", log: ["(browser dev mode – same-origin proxy)"] };
  return invoke("backend_status");
}

export async function restartBackend(): Promise<void> {
  if (!inTauri) return;
  await invoke("restart_backend");
}

export function onBackendStatus(cb: (s: BackendStatus) => void): () => void {
  if (!inTauri) return () => {};
  let un: UnlistenFn | null = null;
  listen<BackendStatus>("backend-status", (e) => cb(e.payload)).then((u) => (un = u));
  return () => un?.();
}

export function onBackendLog(cb: (line: string) => void): () => void {
  if (!inTauri) return () => {};
  let un: UnlistenFn | null = null;
  listen<string>("backend-log", (e) => cb(e.payload)).then((u) => (un = u));
  return () => un?.();
}

/** Read a File into a FilePart for multipart upload over IPC. */
export async function fileToPart(field: string, file: File): Promise<FilePart> {
  const buf = new Uint8Array(await file.arrayBuffer());
  let s = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < buf.length; i += CHUNK) s += String.fromCharCode(...buf.subarray(i, i + CHUNK));
  return { field, name: file.name, mime: file.type || undefined, data: btoa(s) };
}

/** Shared UI helpers — keep selectors, dates, and overlays consistent. */

export function urlsMatch(a?: string | null, b?: string | null) {
  const n = (u?: string | null) =>
    (u || "")
      .trim()
      .replace(/\/+$/, "")
      .replace(/\/chat\/completions$/i, "")
      .replace(/\/v1$/i, "");
  return !!a && !!b && n(a) === n(b);
}

export function idsEqual(a?: string | null, b?: string | null) {
  const x = (a || "").trim();
  const y = (b || "").trim();
  if (x && y) return x === y;
  return !x && !y;
}

export function isLocalUrl(url: string, category?: string) {
  return /127\.0\.0\.1|localhost|0\.0\.0\.0|\.local\b|192\.168\.|10\.\d+\.|172\.(1[6-9]|2\d|3[01])\./.test(url) || category === "local";
}

export const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

export const metaLabel = isMac ? "⌘" : "Ctrl";

export function prefersReducedMotion() {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export async function openExternal(href: string) {
  try {
    const { inTauri } = await import("./ipc");
    if (inTauri) {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(href);
      return;
    }
  } catch {
    /* fall through */
  }
  window.open(href, "_blank", "noopener,noreferrer");
}

export function pad2(n: number) {
  return String(n).padStart(2, "0");
}

/** Local calendar day key — never use toISOString().slice(0,10). */
export function localYmd(d: Date) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function parseDate(iso?: string | null) {
  if (!iso) return new Date(NaN);
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 0), Number(m[5] || 0));
  return new Date(iso);
}

export function downloadText(filename: string, text: string, mime = "text/plain") {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function downloadBase64(filename: string, b64: string, mime = "application/octet-stream") {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([arr], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function shortModel(id?: string | null) {
  if (!id) return "model";
  return id.split("/").pop() || id;
}

export function displayModel(id: string, display?: string) {
  if (display) return display;
  const leaf = shortModel(id).toLowerCase();
  if (leaf === "psd" || leaf.startsWith("psd-coder")) return leaf === "psd" ? "PSD" : `PSD · ${shortModel(id)}`;
  return shortModel(id);
}

export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

export const THEMES = [
  { id: "dark", label: "Dark" },
  { id: "light", label: "Light" },
  { id: "midnight", label: "Midnight" },
  { id: "paper", label: "Paper" },
  { id: "forest", label: "Forest" },
  { id: "ocean", label: "Ocean" },
] as const;

export type ThemeId = (typeof THEMES)[number]["id"];

export function applyTheme(theme: string) {
  document.documentElement.dataset.theme = theme === "light" || theme === "paper" ? "light" : "dark";
  if (theme !== "dark" && theme !== "light") document.documentElement.dataset.skin = theme;
  else delete document.documentElement.dataset.skin;
}

export function applyDensity(d: "comfortable" | "compact") {
  document.documentElement.dataset.density = d;
}

export function applyFontScale(n: number) {
  document.documentElement.style.fontSize = `${n}px`;
}

import { useEffect, useState } from "react";
import { inTauri, request } from "../lib/ipc";

/** Images served by the backend (/api/...) must be fetched over IPC inside Tauri. */
export function ApiImage({
  src,
  alt,
  className,
  style,
}: {
  src?: string;
  alt?: string;
  className?: string;
  style?: React.CSSProperties;
}) {
  const [url, setUrl] = useState<string | undefined>(src && (!inTauri || !src.startsWith("/")) ? src : undefined);
  useEffect(() => {
    if (!src) return;
    if (!inTauri || !src.startsWith("/")) {
      setUrl(src);
      return;
    }
    let alive = true;
    request({ method: "GET", path: src }).then((r) => {
      if (!alive || !r.base64) return;
      setUrl(`data:${r.headers["content-type"] || "image/png"};base64,${r.base64}`);
    });
    return () => {
      alive = false;
    };
  }, [src]);
  if (!url)
    return <span className={`shimmer inline-block rounded-xl ${className || ""}`} style={{ background: "var(--bg-sunken)", minHeight: 80, ...style }} />;
  return <img src={url} alt={alt || ""} className={className} style={style} />;
}

export function Empty({
  icon,
  title,
  hint,
  action,
}: {
  icon: React.ReactNode;
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty">
      <div className="mb-1 opacity-70">{icon}</div>
      <h3>{title}</h3>
      {hint && <p className="max-w-sm text-[13px]">{hint}</p>}
      {action}
    </div>
  );
}

export function PanelHead({
  icon,
  title,
  children,
}: {
  icon?: React.ReactNode;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="panel-head">
      {icon && <span style={{ color: "var(--accent)" }}>{icon}</span>}
      <h2>{title}</h2>
      <div className="ml-auto flex items-center gap-1.5">{children}</div>
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { Images, Heart, Trash2, Upload, Search, X } from "lucide-react";
import { gallery as api, type GalleryImage } from "../lib/api";
import { useApp } from "../store/app";
import { ApiImage, Empty, PanelHead } from "./media";

export default function Gallery() {
  const toast = useApp((s) => s.toast);
  const [items, setItems] = useState<GalleryImage[]>([]);
  const [q, setQ] = useState("");
  const [favOnly, setFavOnly] = useState(false);
  const [open, setOpen] = useState<GalleryImage | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    try {
      const r = await api.library({ search: q || undefined, favorites: favOnly, limit: 60 });
      setItems(r.items || []);
    } catch (e: any) {
      toast(e.message || "Could not load gallery", "error");
      setItems([]);
    }
  };
  useEffect(() => {
    load();
  }, [favOnly]);

  const upload = async (files: FileList | null) => {
    if (!files?.length) return;
    try {
      for (const f of Array.from(files)) await api.upload(f);
      toast("Uploaded", "success");
      load();
    } catch (e: any) {
      toast(e.message || "Upload failed", "error");
    }
  };

  return (
    <section className="panel">
      <PanelHead icon={<Images size={16} />} title="Gallery">
        <div className="relative">
          <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
          <input className="input h-8 w-48 pl-8 text-[13px]" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
        </div>
        <button className="pill" data-on={favOnly} onClick={() => setFavOnly((v) => !v)}>
          <Heart size={12} /> Favorites
        </button>
        <input ref={fileRef} type="file" accept="image/*" multiple hidden onChange={(e) => upload(e.target.files)} />
        <button className="btn btn-primary h-8" onClick={() => fileRef.current?.click()}>
          <Upload size={14} /> Upload
        </button>
      </PanelHead>
      <div className="panel-body">
        {items.length === 0 && (
          <Empty icon={<Images size={36} />} title="Gallery is empty" hint="Images you generate in chat, or upload here, land in this library." action={<button className="btn btn-primary mt-2" onClick={() => fileRef.current?.click()}>Upload an image</button>} />
        )}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {items.map((img) => (
            <button key={img.id} className="group relative overflow-hidden rounded-2xl" style={{ border: "1px solid var(--border)", background: "var(--bg-sunken)" }} onClick={() => setOpen(img)}>
              <ApiImage src={img.url} alt={img.prompt || ""} className="aspect-square w-full object-cover" />
              {img.favorite && (
                <span className="absolute right-2 top-2 rounded-full p-1" style={{ background: "var(--accent-soft)", color: "var(--accent)" }}>
                  <Heart size={12} fill="currentColor" />
                </span>
              )}
              {img.prompt && (
                <span className="absolute inset-x-0 bottom-0 truncate bg-black/50 px-2 py-1 text-left text-[11px] text-white opacity-0 transition-opacity group-hover:opacity-100">
                  {img.prompt}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>
      {open && (
        <div className="absolute inset-0 z-40 flex items-center justify-center p-8" style={{ background: "rgba(0,0,0,.55)", backdropFilter: "blur(8px)" }} onMouseDown={(e) => e.target === e.currentTarget && setOpen(null)}>
          <div className="glass max-h-full w-full max-w-3xl overflow-auto rounded-3xl p-4">
            <div className="mb-3 flex items-center gap-2">
              <div className="min-w-0 flex-1 truncate text-[13px]" style={{ color: "var(--muted)" }}>
                {open.prompt || open.filename}
              </div>
              <button className="icon-btn" title="Favorite" onClick={async () => { await api.favorite(open.id); setOpen({ ...open, favorite: !open.favorite }); load(); }}>
                <Heart size={16} fill={open.favorite ? "currentColor" : "none"} style={{ color: open.favorite ? "var(--accent)" : undefined }} />
              </button>
              <button className="icon-btn hover:!text-red-400" title="Delete" onClick={async () => { await api.remove(open.id); setOpen(null); load(); }}>
                <Trash2 size={16} />
              </button>
              <button className="icon-btn" onClick={() => setOpen(null)}>
                <X size={16} />
              </button>
            </div>
            <ApiImage src={open.url} alt="" className="max-h-[70vh] w-full rounded-2xl object-contain" />
          </div>
        </div>
      )}
    </section>
  );
}

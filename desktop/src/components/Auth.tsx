import { useEffect, useState, type FormEvent } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Eye, EyeOff, LogIn, ShieldCheck, UserPlus } from "lucide-react";
import { auth } from "../lib/api";
import { useApp } from "../store/app";

export default function Auth({ mode }: { mode: "setup" | "login" }) {
  const refreshAuth = useApp((s) => s.refreshAuth);
  const authStatus = useApp((s) => s.authStatus);
  const [username, setUsername] = useState(() => {
    try {
      return localStorage.getItem("psd.username") || "";
    } catch {
      return "";
    }
  });
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [minLen, setMinLen] = useState(8);
  const [signup, setSignup] = useState(false);

  useEffect(() => {
    auth.policy().then((p) => setMinLen(p.password_min_length)).catch(() => {});
  }, []);

  const creating = mode === "setup" || signup;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (creating) {
      if (password.length < minLen) return setError(`Password must be at least ${minLen} characters`);
      if (password !== confirm) return setError("Passwords do not match");
    }
    setBusy(true);
    try {
      if (mode === "setup") await auth.setup(username.trim(), password);
      else if (signup) await auth.signup(username.trim(), password);
      try {
        localStorage.setItem("psd.username", username.trim());
      } catch {
        /* ignore */
      }
      const r = await auth.login(username.trim(), password, totp || undefined);
      if (r && r.requires_totp) {
        setNeedTotp(true);
        setBusy(false);
        return;
      }
      await refreshAuth();
    } catch (err: any) {
      setError(err?.message || "Something went wrong");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative z-10 flex h-full items-center justify-center px-6">
      <motion.form
        onSubmit={submit}
        initial={{ opacity: 0, y: 14, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.4, ease: "easeOut" }}
        className="glass w-full max-w-sm rounded-3xl p-7"
        style={{ boxShadow: "var(--shadow)" }}
      >
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <img src="/icon.png" alt="" className="h-14 w-14 rounded-2xl" draggable={false} />
          <div>
            <h1 className="text-xl font-semibold tracking-tight">{mode === "setup" ? "Create your admin account" : signup ? "Create an account" : "Welcome back"}</h1>
            <p className="mt-1 text-sm" style={{ color: "var(--muted)" }}>
              {mode === "setup" ? "This is the first launch. Everything stays on this device." : "Sign in to continue to psd.ai"}
            </p>
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium" style={{ color: "var(--muted)" }}>
              Username
            </span>
            <input className="input" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="admin" disabled={needTotp} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium" style={{ color: "var(--muted)" }}>
              Password
            </span>
            <div className="relative">
              <input className="input pr-11" type={show ? "text" : "password"} autoComplete={creating ? "new-password" : "current-password"} value={password} onChange={(e) => setPassword(e.target.value)} placeholder={creating ? `At least ${minLen} characters` : "••••••••"} disabled={needTotp} />
              <button type="button" tabIndex={-1} className="icon-btn absolute right-1 top-1/2 h-8 w-8 -translate-y-1/2" onClick={() => setShow((v) => !v)}>
                {show ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
          </label>
          <AnimatePresence initial={false}>
            {creating && (
              <motion.label key="confirm" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} className="flex flex-col gap-1.5 overflow-hidden">
                <span className="text-xs font-medium" style={{ color: "var(--muted)" }}>
                  Confirm password
                </span>
                <input className="input" type={show ? "text" : "password"} autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              </motion.label>
            )}
            {needTotp && (
              <motion.label key="totp" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} className="flex flex-col gap-1.5 overflow-hidden">
                <span className="flex items-center gap-1.5 text-xs font-medium" style={{ color: "var(--muted)" }}>
                  <ShieldCheck size={13} /> Two-factor code
                </span>
                <input className="input tracking-[0.3em]" inputMode="numeric" autoFocus maxLength={8} value={totp} onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))} placeholder="123456" />
              </motion.label>
            )}
          </AnimatePresence>

          <AnimatePresence>
            {error && (
              <motion.p key="err" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="rounded-xl px-3 py-2 text-sm text-red-400" style={{ background: "rgba(239,68,68,0.1)" }}>
                {error}
              </motion.p>
            )}
          </AnimatePresence>

          {creating && password && (
            <div className="h-1.5 overflow-hidden rounded-full" style={{ background: "var(--bg-sunken)" }}>
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.min(100, password.length * 8)}%`,
                  background: password.length < minLen ? "#f87171" : password.length < minLen + 4 ? "#e5c07b" : "#34d399",
                }}
              />
            </div>
          )}
          <button className="btn btn-primary mt-1 h-11 w-full text-[15px]" disabled={busy || !username || !password}>
            {busy ? (
              <span className="dots">
                <span />
                <span />
                <span />
              </span>
            ) : creating ? (
              <>
                <UserPlus size={16} /> Create account
              </>
            ) : (
              <>
                <LogIn size={16} /> Sign in
              </>
            )}
          </button>

          {mode === "login" && authStatus?.signup_enabled && (
            <button type="button" className="btn btn-ghost text-xs" onClick={() => setSignup((v) => !v)}>
              {signup ? "Already have an account? Sign in" : "Need an account? Sign up"}
            </button>
          )}
        </div>
      </motion.form>
    </div>
  );
}

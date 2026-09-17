import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
  info: string;
}

/**
 * Last line of defence: a render-time crash anywhere in the tree used to leave
 * a completely blank window with no clue. Show the error instead, with a
 * one-click reload.
 */
export default class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null, info: "" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[psd.ai] UI crashed:", error, info.componentStack);
    this.setState({ info: info.componentStack || "" });
  }

  render() {
    if (!this.state.error) return this.props.children;
    const detail = `${this.state.error.name}: ${this.state.error.message}\n${this.state.error.stack || ""}\n${this.state.info}`;
    return (
      <div className="flex h-full w-full items-center justify-center p-8" style={{ background: "var(--bg)", color: "var(--text)" }}>
        <div className="glass w-full max-w-2xl rounded-3xl p-6" style={{ boxShadow: "var(--shadow)" }}>
          <h1 className="text-lg font-semibold">Something went wrong in the interface</h1>
          <p className="mt-1 text-[13px]" style={{ color: "var(--muted)" }}>
            The engine is still running — this is a display error. Reload to continue; if it keeps happening, copy the details below and report them.
          </p>
          <pre className="selectable mt-4 max-h-72 overflow-auto whitespace-pre-wrap rounded-xl p-3 text-[11.5px] leading-relaxed" style={{ background: "var(--code-bg)", color: "#dfe3ee", fontFamily: "var(--font-mono)" }}>
            {detail}
          </pre>
          <div className="mt-4 flex gap-2">
            <button className="btn btn-primary" onClick={() => this.setState({ error: null, info: "" })}>
              Try again
            </button>
            <button className="btn" onClick={() => window.location.reload()}>
              Reload interface
            </button>
            <button className="btn" onClick={() => navigator.clipboard.writeText(detail).catch(() => {})}>
              Copy details
            </button>
          </div>
        </div>
      </div>
    );
  }
}

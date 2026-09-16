# psd.ai Claude Code Integration

This directory contains the Claude Code skill bundle for psd.ai.

## User Flow

1. Open psd.ai Settings > Integrations.
2. Add a Claude Agent.
3. Copy the full setup commands shown after the generated token.
4. Toggle the tools Claude is allowed to use.
5. Configure the terminal Claude Code session:

```bash
export PSD_AI_URL=http://your-psd_ai-host:7000
export PSD_AI_API_TOKEN=ody_generated_token
mkdir -p ~/.claude
curl -fsSL -H "Authorization: Bearer $PSD_AI_API_TOKEN" "$PSD_AI_URL/api/claude/plugin.zip" -o /tmp/psd_ai-claude-skill.zip
python3 -m zipfile -e /tmp/psd_ai-claude-skill.zip ~/.claude/
```

Claude Code auto-loads anything under `~/.claude/skills/`, so the `psd_ai` skill is
available in any session that has `PSD_AI_URL` and `PSD_AI_API_TOKEN` in its
environment.

## What's in the bundle

- `skills/psd_ai/SKILL.md` — the skill definition Claude Code reads.
- `skills/psd_ai/scripts/psd_ai_api.py` — small helper that calls the scoped
  `/api/codex/*` endpoints (these are the canonical scope-gated agent API; the
  `codex` path is historic and shared by all agent integrations).

## Scope enforcement

The token is scope-gated. Every tool surface is checked server-side in psd.ai,
so even if Claude tries to call a forbidden endpoint, it gets `403` until the
user enables the matching toggle in Settings > Integrations > Claude Agent.

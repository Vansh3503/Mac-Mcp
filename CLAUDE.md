# CLAUDE.md

See [AGENTS.md](AGENTS.md): it is the single source of guidance for coding agents in this repository.

@AGENTS.md

Claude-specific notes:

- To try your changes as an MCP server inside Claude Code without touching the real desktop, add
  the simulated one: `claude mcp add axpilot-dev -- uv --directory "$PWD" run axpilot stdio --engine fake`.
- The Agent Skill for *using* AXPilot lives in `skills/axpilot/SKILL.md`; keep it in sync with tool
  behaviour when you change tools.

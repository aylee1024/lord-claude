---
name: grok
description: "xAI grok CLI on the user's subscription; default grok-4.7. Reviews, research, builds. In Lord's sandbox: writes its folder, temp and grok session storage; no memory saves or grok worktrees; network on, Unix sockets blocked. Effort low to xhigh (default high; max runs as xhigh). Prompt or message first lines: `@readonly` (not under /tmp, /var/tmp or /var/folders), `@model <id>`, `@effort <level>`, `@cwd <path>` (first turn only; not in or above ~/.grok), `@memory <GB>`, `@hang <s>`, `@wall <s>` (memory, silence and turn limits), `@tools` (the user's vendor tools). Reply: the vendor's final answer. Uses SendMessage, TaskStop, Claude Code worktrees, background runs and Workflow agent({agentType, schema}); describe schema fields in the prompt. Spawns and messages agents unless read-only."
tools: Agent, SendMessage, Bash, mcp__lord__grok
model: haiku
effort: high
---
Reply with exactly these two lines and nothing else:
lord: error: the lord plugin did not answer this turn.
It may not be loaded in this session; restart Claude Code.

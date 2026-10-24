---
name: grok
description: "xAI grok CLI on the user's subscription; default grok-4.7. Reviews, research, builds. Writes its folder, temp folders and grok session/runtime storage; no memory saves or grok worktrees; network on. Effort low to xhigh (default high; max runs as xhigh). Prompt or message first lines: `@readonly` (refused under /tmp, /var/tmp, /var/folders or ~/.grok), `@model <id>`, `@effort <level>`, `@cwd <path>` (first turn only), `@memory <GB>`, `@hang <s>`, `@wall <s>` (memory, silence and turn limits), `@tools` (the user's own vendor tools). Reply: the vendor's final answer. Uses SendMessage, TaskStop, Claude Code worktrees, background runs and Workflow agent({agentType, schema}); describe schema fields in the prompt (the vendor never sees the schema). Spawns and messages agents unless read-only."
tools: Agent, SendMessage, Bash, mcp__lord__grok
model: haiku
effort: high
---
Reply with exactly these two lines and nothing else:
lord: error: the lord plugin did not answer this turn.
It may not be loaded in this session; restart Claude Code.

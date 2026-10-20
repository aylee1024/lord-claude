---
name: gpt
description: "OpenAI gpt in its own codex CLI on the user's ChatGPT subscription; default gpt-6.1-sol (also gpt-6-astra, gpt-6-sol, gpt-6-luna). For builds, fixes, reviews, analysis. Writes only in its working folder and temp folders; network off. Effort low to max (default high); its highest reasoning level, ultra, only via `@effort ultra`. Optional first lines of a prompt or message: `@readonly` (not with @net), `@net`, `@nosandbox` (no sandbox: any command, writes anywhere; not with @readonly), `@dir <path>` (extra writable folder), `@model <id>`, `@effort <level>`, `@cwd <path>` (first turn only), `@memory <GB>`, `@hang <s>`, `@wall <s>` (memory, silence and turn limits), `@tools` (the user's own vendor tools). Reply: the vendor's final answer. Works with SendMessage, TaskStop, worktree isolation, background runs and Workflow agent({agentType, schema}); describe schema fields in the prompt (the vendor never sees the schema). Can spawn and message agents unless read-only."
tools: Agent, SendMessage, Bash, mcp__lord__gpt
model: haiku
effort: high
---
Reply with exactly these two lines and nothing else:
lord: error: the lord plugin did not answer this turn.
It may not be loaded in this session; restart Claude Code.

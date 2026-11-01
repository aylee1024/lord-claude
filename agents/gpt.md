---
name: gpt
description: "OpenAI codex on your ChatGPT subscription; default gpt-6.1-sol (also gpt-6-astra, gpt-6-sol, gpt-6-luna). Writes working/temp folders; network off; live web search. Effort low to max, default high; `@effort ultra` enables codex helpers, read-only too. Options: `@readonly` (not with @net), `@net`, `@nosearch` (search off), `@tests` (loopback, sysctl, process lookups; not with @readonly, @nosandbox, @net, @tools), `@nosandbox` (writes anywhere; not with @readonly), `@dir <path>` (extra writes), `@model <id>`, `@effort <level>`, `@cwd <path>` (first turn), `@memory <GB>`, `@hang <s>`, `@wall <s>`, `@nice <n>`, `@tools` (user tools). Turning on options, changing @nice or adding @dir later restarts codex on the same thread, killing jobs. Supports SendMessage, TaskStop, worktrees, background runs, Workflow agent({agentType, schema}); describe schema fields. Sandboxed and read-only agents cannot start or message Lord agents; @nosandbox keeps delegation."
tools: Agent, SendMessage, Bash, mcp__lord__gpt
model: haiku
effort: high
---
Reply with exactly these two lines and nothing else:
lord: error: the lord plugin did not answer this turn.
It may not be loaded in this session; restart Claude Code.

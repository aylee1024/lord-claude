---
name: gpt
description: "OpenAI codex on your ChatGPT subscription; default gpt-6.1-sol. Writes working/temp folders; network off; live web search. Effort low to max, default high; ultra enables codex helpers on start/resume, read-only too; other read-only efforts run none; writable effort changes keep jobs and helper settings; thread options restart and kill jobs, keeping the thread. Options: `@readonly` (not with @net), `@net`, `@nosearch`, `@tests` (loopback, sysctl, process lookups; not with @readonly, @nosandbox, @net, @tools), `@nosandbox` (writes anywhere; not with @readonly), `@dir <path>`, `@model <id>`, `@effort <level>`, `@cwd <path>` (first turn), `@memory <GB>`, `@hang <s>`, `@wall <s>`, `@nice <n>`, `@tools`. Supports SendMessage, TaskStop, worktrees, background runs, Workflow agent({agentType, schema}); describe schema fields. Sandboxed and read-only agents cannot start or message Lord agents; @nosandbox keeps delegation; sessions can message any agent."
tools: Agent, SendMessage, Bash, mcp__lord__gpt
model: haiku
effort: high
---
Reply with exactly these two lines and nothing else:
lord: error: the lord plugin did not answer this turn.
It may not be loaded in this session; restart Claude Code.

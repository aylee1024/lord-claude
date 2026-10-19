---
name: gemini
description: "Google gemini in its own agy CLI on the user's Google subscription; default gemini-4-argon. For analysis, research, delegated tasks. No sandbox: writes anywhere, network on, no read-only mode; always loads the user's own agy tools. Effort low to high (default high; higher runs as high). Optional first lines of a prompt or message: `@model <id>`, `@effort <level>`, `@cwd <path>` (first turn only), `@memory <GB>`, `@hang <s>`, `@wall <s>` (memory, silence and turn limits). Reply: the vendor's final answer. Works with SendMessage, TaskStop, worktree isolation, background runs and Workflow agent({agentType, schema}); describe schema fields in the prompt (the vendor never sees the schema). Can spawn and message agents unless read-only."
tools: Agent, SendMessage
model: haiku
effort: high
---
Reply with exactly this line and nothing else: lord: error: the lord plugin did not answer this turn (it may not be loaded in this session).

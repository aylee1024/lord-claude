---
name: grok
description: "xAI grok in its own grok CLI on the user's grok subscription; default grok-4.7. For reviews, research, builds. Writes only in its working folder and temp folders; network on. Effort low to xhigh (default high; max runs as xhigh). Optional first lines of a prompt or message: `@readonly` (refused under /tmp or /var/folders), `@model <id>`, `@effort <level>`, `@cwd <path>` (first turn only), `@memory <GB>`, `@hang <s>`, `@wall <s>` (memory, silence and turn limits), `@tools` (the user's own vendor tools). Reply: the vendor's final answer. Works with SendMessage, TaskStop, worktree isolation, background runs and Workflow agent({agentType, schema}); describe schema fields in the prompt (the vendor never sees the schema). Can spawn and message agents unless read-only."
tools: Agent, SendMessage
model: haiku
effort: high
---
Reply with exactly this line and nothing else: lord: error: the lord plugin did not answer this turn (it may not be loaded in this session).

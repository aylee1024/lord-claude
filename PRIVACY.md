# Privacy

Lord collects no data and has no server. It runs on your computer and makes no network requests of its own.

**What leaves your computer.** When Claude Code gives a Lord agent a task, Lord passes the task's text to the vendor command-line program you chose (OpenAI's `codex`, xAI's `grok` or Google's `agy`). That program sends it to its vendor under your own account, and the vendor's privacy policy applies:

- OpenAI: https://openai.com/policies/privacy-policy/
- xAI: https://x.ai/legal/privacy-policy
- Google: https://policies.google.com/privacy

The vendor program can read and act on files in the agent's working folder with its own tools, as it would if you ran it yourself. Lord removes secret-like environment variables (names containing KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL, and bearer values) before it starts a vendor program, and never reads vendor login files.

**What stays on your computer**, under `~/.claude/lord/`:

- `sessions.jsonl`: one line per agent (agent id, vendor session id, model, effort, working folder, options, last token counts). Task text is never stored here. Lines unused for 14 days are removed.
- `logs/<agentId>.log`: the commands and file actions the vendor reported for that agent, with secret values and common token shapes redacted, at most 4 MB each, removed with the agent's line.
- `lordd.log`: the daemon's own log, at most 1 MB plus one older copy.

Vendor conversation history is kept by each vendor program in its usual place, as when you use it directly.

**Contact.** Questions and reports: https://github.com/aylee1024/lord-claude/issues

# Privacy

Lord collects no data and has no server. It runs on your computer and makes no network requests of its own.

**What leaves your computer.** When Claude Code gives a Lord agent a task, Lord passes the task's text to the vendor command-line program you chose (OpenAI's `codex`, xAI's `grok` or Google's `agy`). That program sends it to its vendor under your own account, and the vendor's privacy policy applies:

- OpenAI: https://openai.com/policies/privacy-policy/
- xAI: https://x.ai/legal/privacy-policy
- Google: https://policies.google.com/privacy

The vendor program reads and acts on files with its own tools, as it would if you ran it yourself, within its sandbox: gpt writes its working folder, `@dir` folders and temp folders; grok its working folder, temp folders and `~/.grok`; gemini (agy) has no sandbox and can write anywhere. Lord removes secret-like environment variables (names containing KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL, and bearer values) before it starts a vendor program, and never reads vendor login files.

**What stays on your computer**, under `~/.claude/lord/`:

- `journal.jsonl` (`sessions.jsonl` before 2.0.18): one line per agent (agent id, vendor session id, model, effort, working folder, options, last token counts). Task text is never stored here. Lines unused for 14 days are removed.
- `logs/<agentId>.log`: the commands and file actions the vendor reported for that agent (and, since 2.2.0, those of codex's own helper agents, the settings each turn ran with, and the first 300 characters of each message sent to the agent while it worked), and any turn error, with secret values and common token shapes redacted, at most 4 MB each, with one earlier 4 MB file kept as `<agentId>.log.1`, removed with the agent's line.
- `logs/<agentId>.answer.md`: the agent's latest final answer, with secrets redacted, removed with the agent's line.
- `logs/<agentId>.json`, `logs/<agentId>.answers/` and `logs/<agentId>.reads.txt` (since 2.2.0): the agent's settings and a record of each turn (times, how it ended, counts, usage, the vendor session id and transcript path), each turn's answer (the last 100), and the paths of files a gpt agent and its codex helpers read, searched or edited (paths only, never contents; the list stops at 4 MB), with secrets redacted, removed with the agent's line.
- `codex/` (since 2.1.2): the codex folder of gpt agents, holding codex's own record of their conversations, so the Codex app does not list them. Your codex login, settings, instructions, custom agents, skills, plugins and rules appear in it only as links to `~/.codex` (or `CODEX_HOME`), as does the folder where codex locks tool sign-ins, which Lord creates there if it is missing. Lord creates the links and never opens the login file; it reads the settings file, as before, to leave out your tool servers, and to see whether your codex setup can be shared this way. Archived conversations stay in this folder after Lord's own records of the agent are removed.
- `quota.json`: when a vendor said its quota was spent, the model and when the quota resets; delete it to clear the refusal.
- `allowance.json` (since 2.2.0): codex's last report of your account's allowance for each of its quota buckets (percent used and reset time per window, credits), which codex sends during its turns; Lord asks for nothing more.
- `lordd.log`: the daemon's own log, at most 1 MB plus one older copy.

Claude Code keeps its own transcript of each Lord agent under `~/.claude/projects/`, as it does for a Claude subagent: since 2.1.0 that transcript holds what the vendor wrote and each step it took, with the step's output redacted and cut to its last 2,000 characters.

Vendor conversation history is kept by each vendor program in its usual place, as when you use it directly, except that codex keeps gpt agents' conversations in `~/.claude/lord/codex` (above).

**Contact.** Questions and reports: https://github.com/aylee1024/lord-claude/issues

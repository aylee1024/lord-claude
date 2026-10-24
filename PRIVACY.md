# Privacy

Lord collects no data and has no server. It runs on your computer and makes no internet requests of its own.

**What leaves your computer.** When Claude Code gives a Lord agent a task, Lord passes the task's text to the vendor command-line program you chose (OpenAI's `codex`, xAI's `grok` or Google's `agy`). That program sends it to its vendor under your own account, and the vendor's privacy policy applies:

- OpenAI: https://openai.com/policies/privacy-policy/
- xAI: https://x.ai/legal/privacy-policy
- Google: https://policies.google.com/privacy

The vendor program reads and acts on files with its own tools, as it would if you ran it yourself, within its sandbox: gpt writes its working folder, `@dir` folders and temp folders; grok its working folder (read-only with `@readonly`), temp folders, `~/.grok/sessions` and its own login files; gemini (agy) has no sandbox and can write anywhere. Lord's grok agents cannot save grok memory or use grok's own git worktrees. Lord depends on neither; Claude Code supplies worktree folders, so its worktree isolation still works.

A grok working folder may not be inside `~/.grok` or contain it (`$GROK_HOME` if set). Every grok agent, read-only included, can change another grok agent's saved conversation, because grok keeps all sessions in one writable folder. Lord starts grok with its own sandbox off inside Lord's macOS Seatbelt profile, passed inline. Grok can update `auth.json` and `mcp_credentials.json`, their `.lock` files, and top-level `.<pid>.<counter>.tmp` replacement files in `~/.grok` (`$GROK_HOME` if set). Everything else there stays unwritable except sessions, including `config.toml`, `sandbox.toml`, `trusted_folders.toml` and other storage folders. Both modes allow IP network access, the mDNSResponder Unix socket for DNS and Lord's own daemon socket for grok's Lord tools. All other Unix sockets, including Docker and tmux, stay blocked. Lord's key is unreadable and its folder unwritable. Project hook configs along the repository-root-to-launch path and root `.git`, `.codex` and `.agents` are write-protected, including resolved targets. At each directory on that path, the agent cannot create a `.claude`, `.cursor` or `.grok` folder missing at start, including by rename or symlink. Named hook configs in existing folders stay protected even when missing; ordinary files remain writable where project writes are allowed. A nonregular or hard-linked hook config refuses the start. Lord removes its old sandbox.toml block only when no daemon of another Lord version is running.

`@tests` lets gpt use loopback TCP (every local TCP service), sysctl reads and psutil process lookups with no direct internet access; a local proxy reached over loopback can pass traffic on. The daemon's Unix socket stays blocked, and writes anywhere under Lord's folder are denied, even when it is an `@dir`. The exec-server inherits the app-server's codex home and creates no additional Lord files. Profiles are passed inline, never written to disk; the exec-server gets no Lord MCP token or daemon socket environment value. Lord removes secret-like environment variables (names containing KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL, and bearer values) before it starts a vendor program, and never reads vendor login files.

**What stays on your computer**, under `~/.claude/lord/`:

- `journal.jsonl` (`sessions.jsonl` before 2.0.18): one line per agent (agent id, vendor session id, model, effort, working folder, options, last token counts). Task text is never stored here. Lines unused for 14 days are removed.
- `logs/<agentId>.log`: the commands and file actions the vendor reported for that agent (and, since 2.2.0, those of codex's own helper agents, the settings each turn ran with, and the first 300 characters of each message sent to the agent while it worked), and any turn error, with secret values and common token shapes redacted, at most 4 MB each, with one earlier 4 MB file kept as `<agentId>.log.1`, removed with the agent's line.
- `logs/<agentId>.answer.md`: the agent's latest final answer, with secrets redacted, removed with the agent's line.
- `logs/<agentId>.json`, `logs/<agentId>.answers/` and `logs/<agentId>.reads.txt` (since 2.2.0): the agent's settings and a record of each turn (times, how it ended, counts, usage, the vendor session id and transcript path), each turn's answer (the last 100), and the paths a gpt agent and its codex helpers read, searched or edited in commands codex recognises as reads, listings, searches or edits (not files a program or script opens; paths only, never contents; the list stops at 4 MB), with secrets redacted, removed with the agent's line.
- `codex/` (since 2.1.2): the codex folder of gpt agents, holding codex's own record of their conversations, so the Codex app does not list them. Your codex login, settings, instructions, custom agents, skills, plugins and rules appear in it only as links to `~/.codex` (or `CODEX_HOME`), as does the folder where codex locks tool sign-ins, which Lord creates there if it is missing. Lord creates the links and never opens the login file; it reads the settings file, as before, to leave out your tool servers, and to see whether your codex setup can be shared this way. Archived conversations stay in this folder after Lord's own records of the agent are removed.
- `quota.json`: when a vendor said its quota was spent, the model and when the quota resets; delete it to clear the refusal.
- `allowance.json` (since 2.2.0): codex's last report of your account's allowance for each of its quota buckets (percent used and reset time per window, credits), which codex sends during its turns; Lord asks for nothing more.
- `lordd.log`: the daemon's own log, at most 1 MB plus one older copy.

Claude Code keeps its own transcript of each Lord agent under `~/.claude/projects/`, as it does for a Claude subagent: since 2.1.0 that transcript holds what the vendor wrote and each step it took, with the step's output redacted and cut to its last 2,000 characters.

Vendor conversation history is kept by each vendor program in its usual place, as when you use it directly, except that codex keeps gpt agents' conversations in `~/.claude/lord/codex` (above).

**Contact.** Questions and reports: https://github.com/aylee1024/lord-claude/issues

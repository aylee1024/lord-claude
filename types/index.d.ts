export type LordVerdict = 'sent' | { isError: boolean; text: string }
export type LordLimits = { memory_gb?: number; hang_s?: number; wall_s?: number; tools?: boolean; nice?: number }
export type LordTask = { text: string; error?: string; model?: string; effort?: string; readonly?: boolean; net?: boolean; nosearch?: boolean; nosandbox?: boolean; tests?: boolean; dirs?: string[]; cwd?: string; limits?: LordLimits; notice?: boolean; launch?: { id: string; daemon: string } }
export type LordAgent = { family: string; owned?: boolean; cwd?: string; running?: boolean; model?: string; effort?: string; readonly?: boolean; net?: boolean; nosearch?: boolean; nosandbox?: boolean; tests?: boolean; dirs?: string[]; limits?: LordLimits; opened?: boolean; error?: string; resumeError?: string; resumePending?: boolean; stopCause?: string
  continuing?: boolean; call?: string; lastAnswer?: string; structured?: LordVerdict; corrected?: boolean; worktree?: boolean; pending?: LordTask; notice?: boolean; daemon?: string; row?: string
  handback?: LordVerdict | 'done'; report?: string; auto?: boolean; refused?: boolean }

declare module 'claude-code' {
  interface PluginState {
    // A reload of the plugin's code empties its module variables mid-turn; these carry a turn in flight across one.
    lord: { agent: StateFamily<LordAgent>; start: StateFamily<LordTask | null>; dir: StateFamily<string>; launch: StateFamily<{ daemon: string; agentId?: string } | null> }
  }
}

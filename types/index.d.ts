export type LordVerdict = 'sent' | { isError: boolean; text: string }
export type LordLimits = { memory_gb?: number; hang_s?: number; wall_s?: number; tools?: boolean }
export type LordTask = { text: string; error?: string; model?: string; effort?: string; readonly?: boolean; net?: boolean; nosandbox?: boolean; dirs?: string[]; cwd?: string; limits?: LordLimits; notice?: boolean }
export type LordAgent = { family: string; cwd?: string; running?: boolean; model?: string; effort?: string; readonly?: boolean; net?: boolean; nosandbox?: boolean; dirs?: string[]; limits?: LordLimits; opened?: boolean; error?: string
  continuing?: boolean; call?: string; lastAnswer?: string; structured?: LordVerdict; corrected?: boolean; worktree?: boolean; pending?: LordTask; notice?: boolean; daemon?: string; row?: string
  handback?: LordVerdict | 'done'; report?: string; auto?: boolean; refused?: boolean }

declare module 'claude-code' {
  interface PluginState {
    // A reload of the plugin's code empties its module variables mid-turn; these carry a turn in flight across one.
    lord: { agent: StateFamily<LordAgent>; start: StateFamily<LordTask | null>; dir: StateFamily<string> }
  }
}

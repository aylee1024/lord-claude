export type Band = string | null

declare module 'claude-code' {
  interface PluginState {
    lord: { band: Band }
  }
}

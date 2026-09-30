export interface Health { ok: boolean, status?: number, ms?: number, path?: string, detail?: string }
export interface Proc {
  id: string, unit: string, port: number | null, command: string, runtime: string
  active: string, sub: string, boot: string, restarts: number, pid: number, memory: number | null
  since: string | null, health: Health | null
}
export interface AppInfo {
  name: string, description: string, domains: string[], state: string, user: string, workdir: string
  processes: Proc[], gpus: number[], sandbox: string, legacy: { unit?: string, site?: string } | null, multi: boolean
}
export interface Gpu {
  index: number, uuid: string, name: string, bus: string, minor: number | null, total_mib: number
  used_mib?: number, util?: number, temp?: number, users: { name: string, mib: number }[]
}
export interface Observed { unit: string, active: string, sub: string, memory: number | null, since: string | null }
export interface Overview {
  apps: AppInfo[], invalid: Record<string, string>, observed: Observed[], gpus: Gpu[], time: number
  ui_domain: string | null
  system: { hostname: string, load: number[], cpus: number, mem_total: number, mem_available: number,
    disk_total: number, disk_used: number, uptime_s: number }
}

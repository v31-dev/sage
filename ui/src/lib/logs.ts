export interface ManagerLogEntry {
  ts: string
  level: string
  logger: string
  taskId: string
  message: string
}

const MANAGER_LOG_RE =
  /^\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]*)\]\s*([\s\S]*)$/

export function parseManagerLog(raw: string): ManagerLogEntry {
  const match = raw.match(MANAGER_LOG_RE)

  if (!match) {
    return {
      ts: '',
      level: '',
      logger: '',
      taskId: '',
      message: raw,
    }
  }

  return {
    ts: match[1]?.split(',')[0]?.trim() ?? '',
    level: match[2]?.trim() ?? '',
    logger: match[3]?.trim() ?? '',
    taskId: match[4]?.trim() ?? '',
    message: match[5]?.trim() ?? '',
  }
}
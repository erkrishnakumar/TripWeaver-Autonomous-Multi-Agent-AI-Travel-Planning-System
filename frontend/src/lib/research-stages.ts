import type { AuditLogEntryRead } from '@/lib/types'

// Mirrors RESEARCH_TASK_NAMES / each Task's name= in app/agents/crew.py --
// the backend's task_callback logs "research.{key}_completed" for each of
// these, in this exact order (Process.sequential), so the first one NOT
// yet in the audit log is the stage currently running.
export const RESEARCH_STAGES = [
  { key: 'flight', label: 'Searching flights' },
  { key: 'hotel', label: 'Searching hotels' },
  { key: 'car_rental', label: 'Checking car rentals' },
  { key: 'context', label: 'Weather, visa & ground transport' },
  { key: 'format', label: 'Compiling findings' },
] as const

export type ResearchStageKey = (typeof RESEARCH_STAGES)[number]['key']

export function completedResearchStages(auditLog: AuditLogEntryRead[] | undefined): Set<string> {
  const completed = new Set<string>()
  if (!auditLog) return completed
  for (const entry of auditLog) {
    const match = /^research\.(\w+)_completed$/.exec(entry.event_type)
    if (match) completed.add(match[1])
  }
  return completed
}

export function findStageStartedAt(
  auditLog: AuditLogEntryRead[] | undefined,
  eventType: string,
): Date | null {
  const entry = auditLog?.find((e) => e.event_type === eventType)
  return entry ? new Date(entry.created_at) : null
}

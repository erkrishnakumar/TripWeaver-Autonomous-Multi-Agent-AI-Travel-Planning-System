import { Check, Loader2 } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { completedResearchStages, findStageStartedAt, RESEARCH_STAGES } from '@/lib/research-stages'
import { useElapsedTime } from '@/lib/use-elapsed-time'
import type { AuditLogEntryRead, TripStatus } from '@/lib/types'
import { cn } from '@/lib/utils'

export function ResearchProgress({
  status,
  auditLog,
}: {
  status: TripStatus
  auditLog: AuditLogEntryRead[] | undefined
}) {
  const researchStartedAt = findStageStartedAt(auditLog, 'research_started')
  const planStartedAt = findStageStartedAt(auditLog, 'plan_started')
  const elapsed = useElapsedTime(status === 'planning' ? planStartedAt : researchStartedAt)

  if (status === 'planning') {
    return (
      <Card className="animate-in-up border-primary/20 bg-primary/[0.04]">
        <CardContent className="flex items-center gap-3 py-4">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
            <Loader2 className="h-4 w-4 animate-spin" />
          </span>
          <div>
            <p className="text-sm text-muted-foreground">
              Turning the research into a day-by-day itinerary…
            </p>
            {elapsed && <p className="text-xs text-muted-foreground/70">Elapsed: {elapsed}</p>}
          </div>
        </CardContent>
      </Card>
    )
  }

  if (status === 'draft') {
    return (
      <Card className="animate-in-up border-primary/20 bg-primary/[0.04]">
        <CardContent className="flex items-center gap-3 py-4">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
            <Loader2 className="h-4 w-4 animate-spin" />
          </span>
          <p className="text-sm text-muted-foreground">Getting ready to start research…</p>
        </CardContent>
      </Card>
    )
  }

  if (status !== 'researching') return null

  const completed = completedResearchStages(auditLog)
  const activeIndex = RESEARCH_STAGES.findIndex((s) => !completed.has(s.key))

  return (
    <Card className="animate-in-up border-primary/20 bg-primary/[0.04] shadow-glow">
      <CardContent className="flex flex-col gap-4 py-5">
        <div className="flex items-center justify-between">
          <p className="text-sm font-medium">TripWeaver's AI agents are researching your trip</p>
          {elapsed && (
            <span className="rounded-full bg-muted px-2.5 py-0.5 text-xs text-muted-foreground">
              {elapsed} elapsed
            </span>
          )}
        </div>

        <ol className="flex flex-col gap-3">
          {RESEARCH_STAGES.map((stage, i) => {
            const isDone = completed.has(stage.key)
            const isActive = i === activeIndex
            return (
              <li key={stage.key} className="flex items-center gap-3">
                <span
                  className={cn(
                    'flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs',
                    isDone && 'border-success bg-success/15 text-success',
                    isActive && 'border-primary bg-primary/15 text-primary',
                    !isDone && !isActive && 'border-border text-muted-foreground',
                  )}
                >
                  {isDone ? (
                    <Check className="h-3.5 w-3.5" />
                  ) : isActive ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    i + 1
                  )}
                </span>
                <span
                  className={cn(
                    'text-sm',
                    isDone && 'text-muted-foreground line-through decoration-success/40',
                    isActive && 'font-medium text-foreground',
                    !isDone && !isActive && 'text-muted-foreground',
                  )}
                >
                  {stage.label}
                </span>
              </li>
            )
          })}
        </ol>

        <p className="text-xs text-muted-foreground/70">
          Usually takes a few minutes — this page updates automatically, no need to refresh.
        </p>
      </CardContent>
    </Card>
  )
}

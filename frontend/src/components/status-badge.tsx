import { Badge } from '@/components/ui/badge'
import type { ApprovalDecision, BookingStatus, TripStatus } from '@/lib/types'

const TRIP_VARIANT: Record<TripStatus, 'default' | 'secondary' | 'success' | 'warning' | 'destructive'> = {
  draft: 'secondary',
  researching: 'default',
  planning: 'default',
  awaiting_approval: 'warning',
  approved: 'default',
  booked: 'success',
  cancelled: 'secondary',
  failed: 'destructive',
}

const BOOKING_VARIANT: Record<BookingStatus, 'default' | 'secondary' | 'success' | 'warning' | 'destructive'> = {
  pending_approval: 'warning',
  approved: 'default',
  rejected: 'secondary',
  booked: 'success',
  booking_failed: 'destructive',
  cancelled: 'secondary',
}

const APPROVAL_VARIANT: Record<ApprovalDecision, 'default' | 'secondary' | 'success' | 'warning' | 'destructive'> = {
  pending: 'warning',
  approved: 'success',
  rejected: 'destructive',
}

function humanize(s: string) {
  return s.replace(/_/g, ' ')
}

export function StatusBadge({ status }: { status: TripStatus }) {
  return <Badge variant={TRIP_VARIANT[status]}>{humanize(status)}</Badge>
}

export function BookingStatusBadge({ status }: { status: BookingStatus }) {
  return <Badge variant={BOOKING_VARIANT[status]}>{humanize(status)}</Badge>
}

export function ApprovalBadge({ decision }: { decision: ApprovalDecision }) {
  return <Badge variant={APPROVAL_VARIANT[decision]}>{humanize(decision)}</Badge>
}

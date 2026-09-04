import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, CalendarDays, Loader2, RefreshCw, Users, Wallet } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { BookingCard } from '@/components/booking-card'
import { ResearchProgress } from '@/components/research-progress'
import { StatusBadge } from '@/components/status-badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { extractErrorMessage, tripsApi } from '@/lib/api'
import { rememberTrip } from '@/lib/trip-history'

function humanizeEventType(eventType: string): string {
  return eventType
    .replace(/^research\./, 'Research: ')
    .replace(/^agent\./, '')
    .replace(/_/g, ' ')
}

const ACTIVE_STATUSES = new Set(['draft', 'researching', 'planning'])

export function TripDetailPage() {
  const { tripId } = useParams<{ tripId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  // POST /trips/{id}/proceed only enqueues propose_trip_bookings -- the
  // trip's status stays "awaiting_approval" until that background job
  // actually finishes re-verifying everything with the provider, which can
  // take a while. Without this flag, the polling below would (correctly)
  // see "awaiting_approval" both immediately before AND after the click and
  // never realize it should keep watching for the status to actually change.
  const [isProposing, setIsProposing] = useState(false)

  const tripQuery = useQuery({
    queryKey: ['trip', tripId],
    queryFn: () => tripsApi.get(tripId!),
    enabled: !!tripId,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (!status) return false
      if (ACTIVE_STATUSES.has(status)) return 4000
      if (status === 'awaiting_approval' && isProposing) return 3000
      return false
    },
  })

  useEffect(() => {
    if (isProposing && tripQuery.data && tripQuery.data.status !== 'awaiting_approval') {
      // The background propose job just resolved (approved/failed) --
      // pull in whatever it actually produced instead of waiting for the
      // next unrelated refetch.
      setIsProposing(false)
      queryClient.invalidateQueries({ queryKey: ['bookings', tripId] })
      queryClient.invalidateQueries({ queryKey: ['audit-log', tripId] })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tripQuery.data?.status])

  const bookingsQuery = useQuery({
    queryKey: ['bookings', tripId],
    queryFn: () => tripsApi.listBookings(tripId!),
    enabled: !!tripId && !!tripQuery.data && tripQuery.data.status !== 'draft',
  })

  const auditLogQuery = useQuery({
    queryKey: ['audit-log', tripId],
    queryFn: () => tripsApi.getAuditLog(tripId!),
    enabled: !!tripId,
    refetchInterval: () => {
      const status = tripQuery.data?.status
      if (!status) return false
      if (ACTIVE_STATUSES.has(status)) return 4000
      if (status === 'awaiting_approval' && isProposing) return 3000
      return false
    },
  })

  useEffect(() => {
    if (tripId) rememberTrip(tripId)
  }, [tripId])

  const proceedMutation = useMutation({
    mutationFn: () => tripsApi.proceed(tripId!),
    onSuccess: () => {
      toast.success("Approved — verifying real prices and proposing bookings…")
      setIsProposing(true)
      queryClient.invalidateQueries({ queryKey: ['trip', tripId] })
      queryClient.invalidateQueries({ queryKey: ['audit-log', tripId] })
    },
    onError: (err) => toast.error(extractErrorMessage(err, 'Could not proceed.')),
  })

  if (!tripId) return null

  if (tripQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground">
        <RefreshCw className="h-4 w-4 animate-spin" />
        Loading trip…
      </div>
    )
  }

  if (tripQuery.isError) {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-destructive">{extractErrorMessage(tripQuery.error, 'Trip not found.')}</p>
        <Button variant="outline" onClick={() => navigate('/')}>
          Back home
        </Button>
      </div>
    )
  }

  const trip = tripQuery.data!

  return (
    <div className="flex flex-col gap-6">
      <button
        onClick={() => navigate('/')}
        className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        All trips
      </button>

      <div className="animate-in-up flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
        <div>
          <h1 className="font-display text-3xl font-extrabold">
            {trip.origin_iata} <span className="text-primary">→</span> {trip.destination_iata}
          </h1>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <CalendarDays className="h-3.5 w-3.5" />
              {trip.depart_date}
              {trip.return_date ? ` – ${trip.return_date}` : ''}
            </span>
            <span className="flex items-center gap-1.5">
              <Users className="h-3.5 w-3.5" />
              {trip.adults} adult{trip.adults > 1 ? 's' : ''}
            </span>
            {trip.max_budget_usd && (
              <span className="flex items-center gap-1.5">
                <Wallet className="h-3.5 w-3.5" />${trip.max_budget_usd}
              </span>
            )}
          </div>
        </div>
        <StatusBadge status={trip.status} />
      </div>

      <ResearchProgress status={trip.status} auditLog={auditLogQuery.data} />

      {trip.status === 'awaiting_approval' && (
        <Card className="animate-in-up border-warning/30 bg-warning/[0.06] shadow-glow">
          <CardHeader>
            <CardTitle className="font-display text-lg">Gate 1: Approve the plan</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {isProposing || proceedMutation.isPending ? (
              <div className="flex items-center gap-3 py-1">
                <Loader2 className="h-4 w-4 animate-spin text-primary" />
                <p className="text-sm text-muted-foreground">
                  Verifying real prices with the provider and proposing bookings — this page
                  updates automatically.
                </p>
              </div>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">
                  Research is done. Approve to have TripWeaver propose specific flight, hotel, and
                  car bookings for you to review.
                </p>
                <div>
                  <Button size="lg" onClick={() => proceedMutation.mutate()}>
                    Approve &amp; propose bookings
                  </Button>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      )}

      {(trip.status === 'approved' || trip.status === 'booked' || trip.status === 'failed') && (
        <div className="animate-in-up">
          <h2 className="mb-3 font-display text-lg font-bold">Proposed bookings</h2>
          {bookingsQuery.isLoading && <p className="text-sm text-muted-foreground">Loading bookings…</p>}
          {bookingsQuery.data && bookingsQuery.data.length === 0 && (
            <p className="text-sm text-muted-foreground">
              {trip.status === 'failed'
                ? "TripWeaver couldn't propose any bookings — see Activity below for why (usually a provider verification failure)."
                : 'No bookings proposed yet — check back shortly.'}
            </p>
          )}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {bookingsQuery.data?.map((booking) => (
              <BookingCard key={booking.booking_id} tripId={tripId} booking={booking} />
            ))}
          </div>
        </div>
      )}

      <div className="animate-in-up">
        <h2 className="mb-3 font-display text-lg font-bold">Activity</h2>
        <Card className="border-border/80">
          <CardContent className="py-4">
            {auditLogQuery.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
            {auditLogQuery.data && auditLogQuery.data.length === 0 && (
              <p className="text-sm text-muted-foreground">No activity yet.</p>
            )}
            <ol className="flex flex-col gap-4">
              {auditLogQuery.data?.map((entry) => (
                <li key={entry.sequence} className="relative flex flex-col gap-1 pl-5">
                  <span className="absolute left-0 top-1.5 h-2 w-2 rounded-full bg-primary" />
                  <span className="absolute left-[3px] top-3.5 bottom-[-1rem] w-px bg-border last:hidden" />
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium capitalize">
                      {humanizeEventType(entry.event_type)}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {new Date(entry.created_at).toLocaleString()}
                    </span>
                  </div>
                  {Object.keys(entry.payload).length > 0 && (
                    <pre className="overflow-x-auto rounded-lg bg-muted p-2.5 text-xs text-muted-foreground">
                      {JSON.stringify(entry.payload, null, 2)}
                    </pre>
                  )}
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

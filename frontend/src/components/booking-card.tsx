import { createThreeDSecureSession, DuffelCardForm, useDuffelCardFormActions } from '@duffel/components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2, Car, PlaneTakeoff } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { BookingStatusBadge } from '@/components/status-badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { approvalsApi, carRentalsApi, extractErrorMessage, tripsApi } from '@/lib/api'
import type { BookingRead, HotelGuestDetails, PassengerDetails } from '@/lib/types'

function emptyPassenger(passengerId: string): PassengerDetails {
  return {
    passenger_id: passengerId,
    title: 'mr',
    gender: 'm',
    given_name: '',
    family_name: '',
    date_of_birth: '',
    email: '',
    phone_number: '',
  }
}

function emptyGuest(): HotelGuestDetails {
  return { given_name: '', family_name: '' }
}

const BOOKING_ICON = { flight: PlaneTakeoff, hotel: Building2, car: Car } as const

export function BookingCard({ tripId, booking }: { tripId: string; booking: BookingRead }) {
  const queryClient = useQueryClient()
  const [approveOpen, setApproveOpen] = useState(false)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [decisionNotes, setDecisionNotes] = useState('')

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['bookings', tripId] })
    queryClient.invalidateQueries({ queryKey: ['audit-log', tripId] })
    queryClient.invalidateQueries({ queryKey: ['trip', tripId] })
  }

  const rejectMutation = useMutation({
    mutationFn: () => approvalsApi.reject(booking.approval_id, { decision_notes: decisionNotes || null }),
    onSuccess: () => {
      toast.success('Booking rejected.')
      setRejectOpen(false)
      invalidate()
    },
    onError: (err) => toast.error(extractErrorMessage(err, 'Could not reject.')),
  })

  const isPending = booking.approval_decision === 'pending'
  const Icon = BOOKING_ICON[booking.booking_type]

  return (
    <Card className="border-border/80 transition-shadow hover:shadow-glow">
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-base capitalize">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Icon className="h-4 w-4" />
          </span>
          {booking.booking_type} booking
        </CardTitle>
        <BookingStatusBadge status={booking.status} />
      </CardHeader>
      <CardContent className="flex flex-col gap-1 text-sm">
        <p>
          Total price:{' '}
          <span className="font-display font-bold text-foreground">
            ${booking.total_price_usd.toFixed(2)}
          </span>
        </p>
        {booking.provider_booking_reference && (
          <p className="text-muted-foreground">Reference: {booking.provider_booking_reference}</p>
        )}
        {booking.failure_reason && (
          <p className="text-destructive">Failure: {booking.failure_reason}</p>
        )}
      </CardContent>
      {isPending && (
        <CardFooter className="gap-2">
          <Button size="sm" onClick={() => setApproveOpen(true)}>
            Approve &amp; book
          </Button>
          <Button size="sm" variant="outline" onClick={() => setRejectOpen(true)}>
            Reject
          </Button>
        </CardFooter>
      )}

      <ApproveDialog
        open={approveOpen}
        onOpenChange={setApproveOpen}
        tripId={tripId}
        booking={booking}
        onDone={invalidate}
      />

      <Dialog open={rejectOpen} onOpenChange={setRejectOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reject this booking?</DialogTitle>
            <DialogDescription>This will mark the booking as rejected. It cannot be undone.</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="notes">Notes (optional)</Label>
            <Textarea
              id="notes"
              value={decisionNotes}
              onChange={(e) => setDecisionNotes(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRejectOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={rejectMutation.isPending}
              onClick={() => rejectMutation.mutate()}
            >
              {rejectMutation.isPending ? 'Rejecting…' : 'Reject booking'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}

function ApproveDialog({
  open,
  onOpenChange,
  tripId,
  booking,
  onDone,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  tripId: string
  booking: BookingRead
  onDone: () => void
}) {
  const confirmInfo = useQuery({
    queryKey: ['confirm-info', tripId, booking.booking_id],
    queryFn: () => tripsApi.getBookingConfirmInfo(tripId, booking.booking_id),
    enabled: open,
  })

  const [passengers, setPassengers] = useState<PassengerDetails[]>([])
  const [guests, setGuests] = useState<HotelGuestDetails[]>([emptyGuest()])
  const [contactEmail, setContactEmail] = useState('')
  const [contactPhone, setContactPhone] = useState('')
  const [threeDSecureSessionId, setThreeDSecureSessionId] = useState<string | null>(null)

  const passengerIds = confirmInfo.data?.passenger_ids ?? []
  useEffect(() => {
    if (booking.booking_type === 'flight' && passengerIds.length > 0) {
      setPassengers(passengerIds.map(emptyPassenger))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmInfo.data])

  useEffect(() => {
    if (!open) setThreeDSecureSessionId(null)
  }, [open])

  const confirmMutation = useMutation({
    mutationFn: () =>
      approvalsApi.confirm(
        booking.approval_id,
        booking.booking_type === 'flight'
          ? { passengers }
          : booking.booking_type === 'hotel'
            ? {
                guests,
                contact_email: contactEmail,
                contact_phone_number: contactPhone,
              }
            : { three_d_secure_session_id: threeDSecureSessionId },
      ),
    onSuccess: (result) => {
      if (result.booking_status === 'booking_failed') {
        toast.error(result.message)
      } else {
        toast.success(result.message)
      }
      onOpenChange(false)
      onDone()
    },
    onError: (err) => toast.error(extractErrorMessage(err, 'Could not confirm booking.')),
  })

  const updatePassenger = (index: number, patch: Partial<PassengerDetails>) => {
    setPassengers((prev) => prev.map((p, i) => (i === index ? { ...p, ...patch } : p)))
  }
  const updateGuest = (index: number, patch: Partial<HotelGuestDetails>) => {
    setGuests((prev) => prev.map((g, i) => (i === index ? { ...g, ...patch } : g)))
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Approve &amp; book</DialogTitle>
          <DialogDescription>{confirmInfo.data?.note ?? 'Loading booking details…'}</DialogDescription>
        </DialogHeader>

        {confirmInfo.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

        {booking.booking_type === 'flight' && passengers.length > 0 && (
          <div className="flex flex-col gap-4">
            {passengers.map((p, i) => (
              <div key={p.passenger_id} className="rounded-md border border-border p-3">
                <p className="mb-2 text-sm font-medium">Passenger {i + 1}</p>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <Label>Title</Label>
                    <Select value={p.title} onValueChange={(v) => updatePassenger(i, { title: v as PassengerDetails['title'] })}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="mr">Mr</SelectItem>
                        <SelectItem value="mrs">Mrs</SelectItem>
                        <SelectItem value="ms">Ms</SelectItem>
                        <SelectItem value="miss">Miss</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label>Gender</Label>
                    <Select value={p.gender} onValueChange={(v) => updatePassenger(i, { gender: v as PassengerDetails['gender'] })}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="m">Male</SelectItem>
                        <SelectItem value="f">Female</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label>Given name</Label>
                    <Input value={p.given_name} onChange={(e) => updatePassenger(i, { given_name: e.target.value })} />
                  </div>
                  <div>
                    <Label>Family name</Label>
                    <Input value={p.family_name} onChange={(e) => updatePassenger(i, { family_name: e.target.value })} />
                  </div>
                  <div>
                    <Label>Date of birth</Label>
                    <Input type="date" value={p.date_of_birth} onChange={(e) => updatePassenger(i, { date_of_birth: e.target.value })} />
                  </div>
                  <div>
                    <Label>Phone</Label>
                    <Input value={p.phone_number} onChange={(e) => updatePassenger(i, { phone_number: e.target.value })} />
                  </div>
                  <div className="col-span-2">
                    <Label>Email</Label>
                    <Input type="email" value={p.email} onChange={(e) => updatePassenger(i, { email: e.target.value })} />
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {booking.booking_type === 'hotel' && confirmInfo.data && (
          <div className="flex flex-col gap-4">
            {guests.map((g, i) => (
              <div key={i} className="grid grid-cols-2 gap-2">
                <div>
                  <Label>Given name</Label>
                  <Input value={g.given_name} onChange={(e) => updateGuest(i, { given_name: e.target.value })} />
                </div>
                <div>
                  <Label>Family name</Label>
                  <Input value={g.family_name} onChange={(e) => updateGuest(i, { family_name: e.target.value })} />
                </div>
              </div>
            ))}
            <Button type="button" size="sm" variant="outline" onClick={() => setGuests((p) => [...p, emptyGuest()])}>
              Add another guest
            </Button>
            <div>
              <Label>Contact email</Label>
              <Input type="email" value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} />
            </div>
            <div>
              <Label>Contact phone</Label>
              <Input value={contactPhone} onChange={(e) => setContactPhone(e.target.value)} />
            </div>
          </div>
        )}

        {booking.booking_type === 'car' && confirmInfo.data?.car_quote_id && (
          <CarPaymentSection
            carQuoteId={confirmInfo.data.car_quote_id}
            sessionId={threeDSecureSessionId}
            onSessionReady={setThreeDSecureSessionId}
          />
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={
              confirmMutation.isPending ||
              confirmInfo.isLoading ||
              (booking.booking_type === 'car' && !threeDSecureSessionId)
            }
            onClick={() => confirmMutation.mutate()}
          >
            {confirmMutation.isPending ? 'Booking…' : 'Confirm & book'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

type CardFormActionState = 'loading-key' | 'ready' | 'verifying' | 'verified' | 'error'

function CarPaymentSection({
  carQuoteId,
  sessionId,
  onSessionReady,
}: {
  carQuoteId: string
  sessionId: string | null
  onSessionReady: (sessionId: string) => void
}) {
  const { ref, createCardForTemporaryUse } = useDuffelCardFormActions()
  const [clientKey, setClientKey] = useState<string | null>(null)
  const [state, setState] = useState<CardFormActionState>('loading-key')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    carRentalsApi
      .getComponentClientKey()
      .then((key) => {
        if (!cancelled) {
          setClientKey(key)
          setState('ready')
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(extractErrorMessage(err, 'Could not start the payment form.'))
          setState('error')
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleCreateCardSuccess = async (data: { id: string }) => {
    if (!clientKey) return
    setState('verifying')
    setError(null)
    try {
      const session = await createThreeDSecureSession(clientKey, data.id, carQuoteId, [], false)
      if (session.status === 'ready_for_payment') {
        onSessionReady(session.id)
        setState('verified')
      } else {
        setError(
          `This card needs extra verification we don't support yet (status: ${session.status}). ` +
            'Use the sandbox test card below instead.',
        )
        setState('error')
      }
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not verify this card.'))
      setState('error')
    }
  }

  if (state === 'loading-key') {
    return <p className="text-sm text-muted-foreground">Loading payment form…</p>
  }

  if (state === 'verified' && sessionId) {
    return (
      <p className="rounded-md border border-success/30 bg-success/10 p-3 text-sm text-success">
        Card verified — ready to book.
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted-foreground">
        Duffel sandbox test card — number <code>4111110116638870</code>, any future expiry, any
        3-digit CVC. Real card details never reach TripWeaver's own server.
      </p>
      {clientKey && (
        <DuffelCardForm
          ref={ref}
          clientKey={clientKey}
          intent="to-create-card-for-temporary-use"
          onCreateCardForTemporaryUseSuccess={handleCreateCardSuccess}
          onCreateCardForTemporaryUseFailure={(err) => {
            setError(err.message)
            setState('error')
          }}
        />
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}
      <Button
        type="button"
        variant="outline"
        disabled={!clientKey || state === 'verifying'}
        onClick={() => createCardForTemporaryUse()}
      >
        {state === 'verifying' ? 'Verifying…' : 'Use this card'}
      </Button>
    </div>
  )
}

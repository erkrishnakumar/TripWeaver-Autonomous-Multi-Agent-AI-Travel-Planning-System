import { useMutation, useQueries } from '@tanstack/react-query'
import { ArrowRight, MapPinned, Plane, Sparkles, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { StatusBadge } from '@/components/status-badge'
import { extractErrorMessage, tripsApi } from '@/lib/api'
import { forgetTrip, getTripHistory, rememberTrip } from '@/lib/trip-history'
import type { TripCreate } from '@/lib/types'

const emptyForm: TripCreate = {
  origin_iata: '',
  destination_iata: '',
  depart_date: '',
  return_date: '',
  adults: 1,
  max_budget_usd: undefined,
  requester_email: '',
  wants_car_rental: false,
}

export function HomePage() {
  const navigate = useNavigate()
  const [form, setForm] = useState<TripCreate>(emptyForm)
  const [jumpToId, setJumpToId] = useState('')
  const [tripHistory, setTripHistory] = useState<string[]>(() => getTripHistory())

  const handleForget = (id: string) => {
    forgetTrip(id)
    setTripHistory(getTripHistory())
  }

  const historyQueries = useQueries({
    queries: tripHistory.map((id) => ({
      queryKey: ['trip', id],
      queryFn: () => tripsApi.get(id),
      retry: false,
    })),
  })

  const createTrip = useMutation({
    mutationFn: (body: TripCreate) =>
      tripsApi.create({
        ...body,
        origin_iata: body.origin_iata.toUpperCase(),
        destination_iata: body.destination_iata.toUpperCase(),
        return_date: body.return_date || null,
        requester_email: body.requester_email || null,
        max_budget_usd: body.max_budget_usd || null,
      }),
    onSuccess: (trip) => {
      rememberTrip(trip.id)
      toast.success('Trip created — research has started.')
      navigate(`/trips/${trip.id}`)
    },
    onError: (err) => toast.error(extractErrorMessage(err, 'Could not create trip.')),
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    createTrip.mutate(form)
  }

  const handleJump = (e: React.FormEvent) => {
    e.preventDefault()
    if (jumpToId.trim()) navigate(`/trips/${jumpToId.trim()}`)
  }

  return (
    <div className="flex flex-col gap-8">
      <div className="animate-in-up flex flex-col items-start gap-3">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/20 bg-primary/10 px-3 py-1 text-xs font-medium text-primary">
          <Sparkles className="h-3.5 w-3.5" />
          Agentic trip planning
        </span>
        <h1 className="flex items-center gap-2.5 font-display text-3xl font-extrabold sm:text-4xl">
          <Plane className="h-8 w-8 shrink-0 text-primary" />
          Plan a new trip
        </h1>
        <p className="max-w-xl text-muted-foreground">
          TripWeaver researches flights, hotels, and cars for you, then waits for your approval
          before booking anything.
        </p>
      </div>

      <Card className="animate-in-up border-border/80 shadow-glow">
        <CardHeader>
          <CardTitle className="font-display text-xl">Trip details</CardTitle>
          <CardDescription>Airport codes are 3-letter IATA codes, e.g. JFK, LHR.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="origin">Origin (IATA)</Label>
              <Input
                id="origin"
                required
                maxLength={3}
                placeholder="JFK"
                value={form.origin_iata}
                onChange={(e) => setForm({ ...form, origin_iata: e.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="destination">Destination (IATA)</Label>
              <Input
                id="destination"
                required
                maxLength={3}
                placeholder="LHR"
                value={form.destination_iata}
                onChange={(e) => setForm({ ...form, destination_iata: e.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="depart">Depart date</Label>
              <Input
                id="depart"
                type="date"
                required
                value={form.depart_date}
                onChange={(e) => setForm({ ...form, depart_date: e.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="return">Return date (optional)</Label>
              <Input
                id="return"
                type="date"
                value={form.return_date ?? ''}
                onChange={(e) => setForm({ ...form, return_date: e.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="adults">Adults</Label>
              <Input
                id="adults"
                type="number"
                min={1}
                value={form.adults}
                onChange={(e) => setForm({ ...form, adults: Number(e.target.value) })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="budget">Max budget (USD, optional)</Label>
              <Input
                id="budget"
                type="number"
                min={0}
                value={form.max_budget_usd ?? ''}
                onChange={(e) =>
                  setForm({
                    ...form,
                    max_budget_usd: e.target.value ? Number(e.target.value) : undefined,
                  })
                }
              />
            </div>
            <div className="flex flex-col gap-1.5 sm:col-span-2">
              <Label htmlFor="requester_email">Requester email (optional)</Label>
              <Input
                id="requester_email"
                type="email"
                value={form.requester_email ?? ''}
                onChange={(e) => setForm({ ...form, requester_email: e.target.value })}
              />
            </div>
            <label className="flex items-center gap-2 text-sm sm:col-span-2">
              <input
                type="checkbox"
                checked={form.wants_car_rental}
                onChange={(e) => setForm({ ...form, wants_car_rental: e.target.checked })}
                className="h-4 w-4 rounded border-border accent-primary"
              />
              I also want a car rental
            </label>
            <div className="sm:col-span-2">
              <Button type="submit" size="lg" disabled={createTrip.isPending} className="gap-2">
                {createTrip.isPending ? 'Creating…' : 'Start planning'}
                {!createTrip.isPending && <ArrowRight className="h-4 w-4" />}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card className="border-border/80">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <MapPinned className="h-4 w-4 text-primary" />
            Jump to a trip
          </CardTitle>
          <CardDescription>Have a trip id? Go straight to it.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleJump} className="flex gap-2">
            <Input
              placeholder="Trip id (UUID)"
              value={jumpToId}
              onChange={(e) => setJumpToId(e.target.value)}
            />
            <Button type="submit" variant="secondary">
              Go
            </Button>
          </form>
        </CardContent>
      </Card>

      {tripHistory.length > 0 && (
        <Card className="border-border/80">
          <CardHeader>
            <CardTitle className="text-base">Recent trips on this device</CardTitle>
            <CardDescription>
              TripWeaver doesn't have a "list my trips" endpoint yet, so this is a local, per-browser
              shortcut list.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col divide-y divide-border">
            {tripHistory.map((id, i) => {
              const q = historyQueries[i]
              const trip = q?.data
              return (
                <div
                  key={id}
                  className="group flex items-center justify-between gap-4 rounded-lg -mx-2 px-2 py-1 transition-colors hover:bg-muted/60"
                >
                  <button
                    onClick={() => navigate(`/trips/${id}`)}
                    className="flex min-w-0 flex-1 items-center gap-3 py-2 text-left"
                  >
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary transition-transform group-hover:scale-105">
                      <Plane className="h-4 w-4" />
                    </span>
                    <div className="min-w-0">
                      <p className="truncate font-medium">
                        {trip ? `${trip.origin_iata} → ${trip.destination_iata}` : id}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {trip ? trip.depart_date : q?.isError ? 'Not found' : 'Loading…'}
                      </p>
                    </div>
                  </button>
                  <div className="flex shrink-0 items-center gap-2">
                    {trip && <StatusBadge status={trip.status} />}
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        handleForget(id)
                      }}
                      aria-label="Remove from recent trips"
                      className="rounded-md p-1.5 text-muted-foreground opacity-0 transition-opacity hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              )
            })}
          </CardContent>
        </Card>
      )}
    </div>
  )
}

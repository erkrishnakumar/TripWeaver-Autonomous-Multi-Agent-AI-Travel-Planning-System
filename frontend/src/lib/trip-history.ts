// The backend has no "list my trips" endpoint (GET /trips/{id} only), so we
// keep a local, per-browser record of trip ids the user has created or
// visited, purely as a navigation convenience.
const KEY = 'tripweaver_trip_history'

export function getTripHistory(): string[] {
  try {
    const raw = localStorage.getItem(KEY)
    return raw ? (JSON.parse(raw) as string[]) : []
  } catch {
    return []
  }
}

export function rememberTrip(tripId: string) {
  const existing = getTripHistory().filter((id) => id !== tripId)
  localStorage.setItem(KEY, JSON.stringify([tripId, ...existing].slice(0, 50)))
}

export function forgetTrip(tripId: string) {
  localStorage.setItem(KEY, JSON.stringify(getTripHistory().filter((id) => id !== tripId)))
}

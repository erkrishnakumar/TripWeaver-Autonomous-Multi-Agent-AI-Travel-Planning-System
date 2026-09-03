import { Plane, ShieldCheck, Sparkles } from 'lucide-react'
import type { ReactNode } from 'react'

const HIGHLIGHTS = [
  { icon: Sparkles, text: 'AI researches flights, hotels & cars for you' },
  { icon: ShieldCheck, text: 'Nothing books until you personally approve it' },
]

export function AuthShell({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto grid max-w-4xl grid-cols-1 items-center gap-10 lg:grid-cols-2">
      <div className="hidden animate-in-up flex-col gap-6 lg:flex">
        <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-primary to-accent text-white shadow-glow">
          <Plane className="h-6 w-6" />
        </span>
        <h1 className="text-4xl font-extrabold leading-tight">
          Plan trips <span className="text-gradient">agentically</span>,<br />
          approve with confidence.
        </h1>
        <p className="max-w-sm text-muted-foreground">
          TripWeaver researches your itinerary end to end, then hands you the final call before a
          single dollar moves.
        </p>
        <ul className="flex flex-col gap-3">
          {HIGHLIGHTS.map(({ icon: Icon, text }) => (
            <li key={text} className="flex items-center gap-3 text-sm">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
                <Icon className="h-4 w-4" />
              </span>
              {text}
            </li>
          ))}
        </ul>
      </div>

      <div className="animate-in-up mx-auto w-full max-w-sm lg:mx-0">{children}</div>
    </div>
  )
}

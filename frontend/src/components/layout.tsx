import { Plane } from 'lucide-react'
import { Link, Outlet } from 'react-router-dom'
import { ProfileMenu } from '@/components/profile-menu'
import { useAuth } from '@/lib/auth-context'

export function Layout() {
  const { isAuthenticated } = useAuth()

  return (
    <div className="relative min-h-screen bg-background">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-[480px] overflow-hidden">
        <div className="bg-grid absolute inset-0" />
        <div className="absolute left-1/2 top-[-180px] h-[420px] w-[720px] -translate-x-1/2 rounded-full bg-primary/20 blur-[110px]" />
      </div>

      <header className="sticky top-0 z-30 border-b border-border/70 bg-background/70 backdrop-blur-md">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-4">
          <Link to="/" className="group flex items-center gap-2 font-display text-lg font-bold">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-accent text-white shadow-soft transition-transform group-hover:scale-105">
              <Plane className="h-4 w-4" />
            </span>
            TripWeaver
          </Link>
          {isAuthenticated && <ProfileMenu />}
        </div>
      </header>

      <main className="relative mx-auto max-w-5xl px-4 py-10">
        <Outlet />
      </main>

      <footer className="relative mt-16 border-t border-border/70 py-8 text-center text-xs text-muted-foreground">
        TripWeaver &middot; agentic trip planning, with a human in the loop.
      </footer>
    </div>
  )
}

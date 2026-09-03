import type { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth-context'

// Auth pages (login/register/etc.) shouldn't be reachable while already
// logged in -- otherwise the header still shows the logged-in user's
// profile next to a "create an account" form, which reads as broken.
export function PublicOnlyRoute({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth()

  if (isAuthenticated) {
    return <Navigate to="/" replace />
  }
  return <>{children}</>
}

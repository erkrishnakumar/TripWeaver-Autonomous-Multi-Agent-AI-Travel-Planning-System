import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { authApi, authStorage } from '@/lib/api'
import type { UserRead } from '@/lib/types'

interface AuthContextValue {
  token: string | null
  user: UserRead | null
  isAuthenticated: boolean
  login: (token: string) => void
  logout: () => void
  setUser: (user: UserRead) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

// Matches the backend's JWT_ACCESS_TOKEN_EXPIRE_MINUTES default (see
// app/config.py) -- the token would stop working server-side at this point
// anyway, so proactively log out client-side instead of waiting for the
// next API call to fail with a 401.
const SESSION_DURATION_MS = 30 * 60 * 1000

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(authStorage.get())
  const [user, setUser] = useState<UserRead | null>(null)
  const logoutTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!token) {
      setUser(null)
      return
    }
    authApi
      .me()
      .then(setUser)
      .catch(() => setUser(null))
  }, [token])

  const clearLogoutTimer = () => {
    if (logoutTimer.current) {
      clearTimeout(logoutTimer.current)
      logoutTimer.current = null
    }
  }

  const logout = () => {
    clearLogoutTimer()
    authStorage.clear()
    setToken(null)
    setUser(null)
  }

  const login = (newToken: string) => {
    authStorage.set(newToken)
    setToken(newToken)
    clearLogoutTimer()
    logoutTimer.current = setTimeout(() => {
      logout()
      toast.info("You've been logged out after 30 minutes. Please log in again.")
    }, SESSION_DURATION_MS)
  }

  useEffect(() => clearLogoutTimer, [])

  return (
    <AuthContext.Provider value={{ token, user, isAuthenticated: !!token, login, logout, setUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

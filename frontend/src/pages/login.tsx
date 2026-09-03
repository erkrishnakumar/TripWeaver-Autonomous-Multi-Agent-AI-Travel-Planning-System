import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { AuthShell } from '@/components/auth-shell'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordInput } from '@/components/ui/password-input'
import { authApi, extractErrorMessage } from '@/lib/api'
import { useAuth } from '@/lib/auth-context'

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)

  const from = (location.state as { from?: Location })?.from?.pathname ?? '/'

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      const { access_token } = await authApi.login(email, password)
      login(access_token)
      navigate(from, { replace: true })
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Login failed.'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthShell>
      <Card className="border-border/80">
        <CardHeader>
          <CardTitle className="font-display text-2xl">Welcome back</CardTitle>
          <CardDescription>Log in to keep planning your trip.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="password">Password</Label>
              <PasswordInput
                id="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <Button type="submit" size="lg" disabled={loading} className="mt-1">
              {loading ? 'Logging in…' : 'Log in'}
            </Button>
          </form>
          <div className="mt-5 flex justify-between text-sm text-muted-foreground">
            <Link to="/forgot-password" className="hover:text-primary hover:underline">
              Forgot password?
            </Link>
            <Link to="/register" className="font-medium text-primary hover:underline">
              Create an account
            </Link>
          </div>
        </CardContent>
      </Card>
    </AuthShell>
  )
}

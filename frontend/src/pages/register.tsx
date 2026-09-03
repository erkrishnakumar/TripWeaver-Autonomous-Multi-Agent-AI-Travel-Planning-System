import { Check, X } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { AuthShell } from '@/components/auth-shell'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordInput } from '@/components/ui/password-input'
import { authApi, extractErrorMessage } from '@/lib/api'
import { useAuth } from '@/lib/auth-context'
import { cn } from '@/lib/utils'
import { PASSWORD_RULES, isPasswordValid } from '@/lib/password'

export function RegisterPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [touchedPassword, setTouchedPassword] = useState(false)
  const [loading, setLoading] = useState(false)

  const passwordValid = isPasswordValid(password)
  const passwordsMatch = password.length > 0 && password === confirmPassword
  const canSubmit = passwordValid && passwordsMatch && email.length > 0

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!canSubmit) {
      setTouchedPassword(true)
      return
    }
    setLoading(true)
    try {
      await authApi.register(email, password, fullName || undefined)
      const { access_token } = await authApi.login(email, password)
      login(access_token)
      toast.success('Account created.')
      navigate('/', { replace: true })
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Registration failed.'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthShell>
      <Card className="border-border/80">
        <CardHeader>
          <CardTitle className="font-display text-2xl">Create an account</CardTitle>
          <CardDescription>Start planning trips with TripWeaver.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="full_name">Full name</Label>
              <Input
                id="full_name"
                autoComplete="name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="password">Password</Label>
              <PasswordInput
                id="password"
                required
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onBlur={() => setTouchedPassword(true)}
              />
              <ul className="mt-1 flex flex-col gap-0.5">
                {PASSWORD_RULES.map((rule) => {
                  const met = rule.test(password)
                  const showState = touchedPassword || password.length > 0
                  return (
                    <li
                      key={rule.label}
                      className={cn(
                        'flex items-center gap-1.5 text-xs',
                        !showState && 'text-muted-foreground',
                        showState && met && 'text-success',
                        showState && !met && 'text-muted-foreground',
                      )}
                    >
                      {showState && met ? (
                        <Check className="h-3 w-3" />
                      ) : (
                        <X className="h-3 w-3 opacity-50" />
                      )}
                      {rule.label}
                    </li>
                  )
                })}
              </ul>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="confirm_password">Confirm password</Label>
              <PasswordInput
                id="confirm_password"
                required
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
              />
              {confirmPassword.length > 0 && !passwordsMatch && (
                <p className="text-xs text-destructive">Passwords don't match.</p>
              )}
            </div>
            <Button type="submit" size="lg" disabled={loading || !canSubmit} className="mt-1">
              {loading ? 'Creating account…' : 'Create account'}
            </Button>
          </form>
          <div className="mt-5 text-sm text-muted-foreground">
            Already have an account?{' '}
            <Link to="/login" className="font-medium text-primary hover:underline">
              Log in
            </Link>
          </div>
        </CardContent>
      </Card>
    </AuthShell>
  )
}

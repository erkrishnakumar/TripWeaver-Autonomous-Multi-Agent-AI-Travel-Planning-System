import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordInput } from '@/components/ui/password-input'
import { authApi, extractErrorMessage } from '@/lib/api'

export function ResetPasswordPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [token, setToken] = useState(searchParams.get('token') ?? '')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      await authApi.resetPassword(token, password)
      toast.success('Password reset. You can now log in.')
      navigate('/login', { replace: true })
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Reset failed.'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-sm animate-in-up pt-8">
      <Card className="border-border/80">
        <CardHeader>
          <CardTitle className="font-display text-2xl">Reset password</CardTitle>
          <CardDescription>Enter your reset token and a new password.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="token">Reset token</Label>
              <Input id="token" required value={token} onChange={(e) => setToken(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="password">New password</Label>
              <PasswordInput
                id="password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <Button type="submit" size="lg" disabled={loading} className="mt-1">
              {loading ? 'Resetting…' : 'Reset password'}
            </Button>
            <Link to="/login" className="text-center text-sm text-muted-foreground hover:underline">
              Back to login
            </Link>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

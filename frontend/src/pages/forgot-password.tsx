import { useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { authApi, extractErrorMessage } from '@/lib/api'

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [devToken, setDevToken] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      const res = await authApi.forgotPassword(email)
      setMessage(res.message)
      setDevToken(res.reset_token ?? null)
    } catch (err) {
      toast.error(extractErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-sm animate-in-up pt-8">
      <Card className="border-border/80">
        <CardHeader>
          <CardTitle className="font-display text-2xl">Forgot password</CardTitle>
          <CardDescription>We'll email you a reset link.</CardDescription>
        </CardHeader>
        <CardContent>
          {message ? (
            <div className="flex flex-col gap-3">
              <p className="text-sm">{message}</p>
              {devToken && (
                <div className="rounded-md border border-dashed border-border p-3 text-xs">
                  <p className="mb-1 font-medium text-muted-foreground">
                    Dev mode (no email provider configured) — reset token:
                  </p>
                  <code className="break-all">{devToken}</code>
                  <Link
                    to={`/reset-password?token=${devToken}`}
                    className="mt-2 block text-primary hover:underline"
                  >
                    Go to reset page →
                  </Link>
                </div>
              )}
              <Link to="/login" className="text-sm text-primary hover:underline">
                Back to login
              </Link>
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <Button type="submit" size="lg" disabled={loading} className="mt-1">
                {loading ? 'Sending…' : 'Send reset link'}
              </Button>
              <Link to="/login" className="text-center text-sm text-muted-foreground hover:underline">
                Back to login
              </Link>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

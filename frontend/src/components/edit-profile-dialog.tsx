import { useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { authApi, extractErrorMessage } from '@/lib/api'
import { useAuth } from '@/lib/auth-context'

export function EditProfileDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { user, setUser } = useAuth()
  const [fullName, setFullName] = useState(user?.full_name ?? '')
  const [loading, setLoading] = useState(false)

  const handleOpenChange = (next: boolean) => {
    if (next) setFullName(user?.full_name ?? '')
    onOpenChange(next)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      const updated = await authApi.updateMe(fullName.trim() || null)
      setUser(updated)
      toast.success('Profile updated.')
      onOpenChange(false)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Could not update profile.'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit profile</DialogTitle>
          <DialogDescription>Update the name shown across TripWeaver.</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="edit_full_name">Full name</Label>
            <Input
              id="edit_full_name"
              autoComplete="name"
              placeholder="Your name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Email</Label>
            <Input value={user?.email ?? ''} disabled />
            <p className="text-xs text-muted-foreground">Email can't be changed here.</p>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? 'Saving…' : 'Save changes'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

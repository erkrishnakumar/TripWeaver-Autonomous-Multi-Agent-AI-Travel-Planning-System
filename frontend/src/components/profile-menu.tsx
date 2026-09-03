import { LogOut, Pencil, User as UserIcon } from 'lucide-react'
import { useState } from 'react'
import { EditProfileDialog } from '@/components/edit-profile-dialog'
import { useAuth } from '@/lib/auth-context'

function initials(name: string) {
  const parts = name.trim().split(/\s+/)
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

export function ProfileMenu() {
  const { user, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)

  if (!user) return null

  const displayName = user.full_name?.trim() || user.email
  const label = user.full_name?.trim() ? initials(user.full_name) : <UserIcon className="h-4 w-4" />

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 rounded-full pr-1 hover:bg-muted"
      >
        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-xs font-medium text-primary-foreground">
          {label}
        </span>
        <span className="hidden text-sm font-medium sm:inline">{displayName}</span>
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-50 mt-2 w-48 rounded-md border border-border bg-card p-1 shadow-md">
            <div className="border-b border-border px-3 py-2">
              <p className="truncate text-sm font-medium">{displayName}</p>
              <p className="truncate text-xs text-muted-foreground">{user.email}</p>
            </div>
            <button
              onClick={() => {
                setEditOpen(true)
                setOpen(false)
              }}
              className="mt-1 flex w-full items-center gap-2 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted"
            >
              <Pencil className="h-4 w-4" />
              Edit profile
            </button>
            <button
              onClick={logout}
              className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted"
            >
              <LogOut className="h-4 w-4" />
              Log out
            </button>
          </div>
        </>
      )}

      <EditProfileDialog open={editOpen} onOpenChange={setEditOpen} />
    </div>
  )
}

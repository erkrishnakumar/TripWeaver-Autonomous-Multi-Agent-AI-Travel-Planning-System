import { useEffect, useState } from 'react'

export function useElapsedTime(since: Date | null): string | null {
  const [, tick] = useState(0)

  useEffect(() => {
    if (!since) return
    const id = setInterval(() => tick((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [since])

  if (!since) return null
  const totalSeconds = Math.max(0, Math.floor((Date.now() - since.getTime()) / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

import { useEffect, useState } from 'react'
import { api, ApiError } from './api'
import Login from './Login'

const ROLE_LABELS = {
  owner: 'Owner',
  teacher: 'Teacher',
  student: 'Student / Parent',
}

export default function App() {
  const [status, setStatus] = useState('loading') // 'loading' | 'ready' | 'error'
  const [user, setUser] = useState(null)
  const [error, setError] = useState('')

  // Runs once when the page opens: get the CSRF cookie, then ask "who am I?"
  useEffect(() => {
    async function start() {
      try {
        await api('/auth/csrf/')
        setUser(await api('/me/'))
      } catch (err) {
        // 403 only means "not logged in yet", which is normal.
        if (!(err instanceof ApiError) || err.status !== 403) {
          setError(err.message)
          setStatus('error')
          return
        }
      }
      setStatus('ready')
    }
    start()
  }, [])

  async function handleLogout() {
    try {
      await api('/auth/logout/', { method: 'POST' })
      setUser(null)
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }

  if (status === 'loading') return <p className="center">Loading…</p>
  if (status === 'error') return <p className="center error">{error}</p>
  if (!user) return <Login onLoggedIn={setUser} />

  const roleLabel = ROLE_LABELS[user.role] ?? user.role
  return (
    <div>
      <header className="topbar">
        <strong>FeeLedger</strong>
        <span>
          {user.username} · {roleLabel}
        </span>
        <button onClick={handleLogout}>Log out</button>
      </header>
      {error && <p className="error">{error}</p>}
      <main>
        <h1>Welcome, {user.username}</h1>
        <p>You are signed in as {roleLabel}. The dashboard comes next.</p>
      </main>
    </div>
  )
}
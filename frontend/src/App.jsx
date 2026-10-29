import { useEffect, useState } from 'react'
import { api, ApiError } from './api'
import Login from './Login'
import Dues from './Dues'
import PlanDetail from './PlanDetail'

const ROLE_LABELS = {
  owner: 'Owner',
  teacher: 'Teacher',
  student: 'Student / Parent',
}

// Which screens exist and which roles may see them. This only decides what to
// SHOW. The real protection is in Django, which refuses anyone not allowed.
const PAGES = [{ id: 'dues', label: 'Fees', roles: ['owner', 'student'] }]

export default function App() {
  const [status, setStatus] = useState('loading') // 'loading' | 'ready' | 'error'
  const [user, setUser] = useState(null)
  const [error, setError] = useState('')
  const [activeId, setActiveId] = useState('dues')
  const [planId, setPlanId] = useState(null) // which fee plan is open, if any

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
      setPlanId(null)
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }

  if (status === 'loading') return <p className="center">Loading…</p>
  if (status === 'error') return <p className="center error">{error}</p>
  if (!user) return <Login onLoggedIn={setUser} />

  const roleLabel = ROLE_LABELS[user.role] ?? user.role
  const pages = PAGES.filter((page) => page.roles.includes(user.role))
  const active = pages.find((page) => page.id === activeId) ?? pages[0]

  return (
    <div>
      <header className="topbar">
        <strong>FeeLedger</strong>
        <nav>
          {pages.map((page) => (
            <button
              key={page.id}
              className={page === active ? 'active' : ''}
              onClick={() => {
                setActiveId(page.id)
                setPlanId(null)
              }}
            >
              {page.label}
            </button>
          ))}
        </nav>
        <span>
          {user.username} · {roleLabel}
        </span>
        <button onClick={handleLogout}>Log out</button>
      </header>
      {error && <p className="error">{error}</p>}
      <main>
        {!active && (
          <p className="empty">
            There are no screens for your role yet. Attendance is coming soon.
          </p>
        )}
        {active?.id === 'dues' &&
          (planId === null ? (
            <Dues role={user.role} onOpen={setPlanId} />
          ) : (
            <PlanDetail planId={planId} role={user.role} onBack={() => setPlanId(null)} />
          ))}
      </main>
    </div>
  )
}
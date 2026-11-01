import { useEffect, useState } from 'react'
import { api } from './api'

const ROLE_LABELS = {
  owner: 'Owner',
  teacher: 'Teacher',
  student: 'Parent / Student',
}
const PAGE_SIZE = 50 // must match PAGE_SIZE in Django's settings

function NewUserForm({ onCreated }) {
  const [username, setUsername] = useState('')
  const [firstName, setFirstName] = useState('')
  const [role, setRole] = useState('teacher')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      const user = await api('/users/', {
        method: 'POST',
        body: {
          username: username.trim(),
          first_name: firstName.trim(),
          role,
          password, // never trimmed: spaces are part of a password
        },
      })
      setUsername('')
      setFirstName('')
      setPassword('')
      onCreated(user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="payment-form" onSubmit={handleSubmit}>
      <label>
        Username
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="off"
          maxLength={150}
          required
        />
      </label>
      <label>
        Name (optional)
        <input
          value={firstName}
          onChange={(e) => setFirstName(e.target.value)}
          maxLength={150}
        />
      </label>
      <label>
        Role
        <select value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="teacher">Teacher</option>
          <option value="student">Parent / Student</option>
        </select>
      </label>
      <label>
        Password
        <input
          type={showPassword ? 'text' : 'password'}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          minLength={8}
          required
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={showPassword}
          onChange={(e) => setShowPassword(e.target.checked)}
        />
        Show password
      </label>
      <button type="submit" disabled={busy}>
        {busy ? 'Creating…' : 'Create account'}
      </button>
      {error && <p className="error full-row">{error}</p>}
    </form>
  )
}

export default function Users() {
  const [role, setRole] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [reloadKey, setReloadKey] = useState(0)
  const [loaded, setLoaded] = useState(null) // { key, data } or { key, error }
  const [notice, setNotice] = useState('')

  // Reload when the filters or page change, or after an account is created.
  // Typing in the search box waits a moment before asking the server.
  useEffect(() => {
    let cancelled = false
    const wanted = `${role}|${search}|${page}`
    const timer = setTimeout(
      () => {
        const query = encodeURIComponent(search.trim())
        api(`/users/?page=${page}&role=${role}&search=${query}`)
          .then((data) => {
            if (!cancelled) setLoaded({ key: wanted, data })
          })
          .catch((err) => {
            if (!cancelled) setLoaded({ key: wanted, error: err.message })
          })
      },
      search ? 300 : 0,
    )
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [role, search, page, reloadKey])

  // The old list stays on screen while a reload is in progress (no flicker).
  const current = loaded && loaded.key === `${role}|${search}|${page}` ? loaded : null
  const results = current?.data?.results ?? []
  const count = current?.data?.count ?? 0
  const totalPages = Math.max(1, Math.ceil(count / PAGE_SIZE))

  function handleCreated(user) {
    setNotice(
      `Account created for ${user.username}. Give them the username and the password you set.`,
    )
    setPage(1)
    setReloadKey((n) => n + 1)
  }

  return (
    <section>
      <h1>Users</h1>
      <p className="empty">
        Create sign-in accounts for teachers and for parents. You choose the first password
        and tell the person. Owner accounts cannot be created here.
      </p>

      <div className="panel">
        <h2>New account</h2>
        <NewUserForm onCreated={handleCreated} />
        {notice && <p className="saved">{notice}</p>}
      </div>

      <h2>All accounts</h2>
      <div className="toolbar">
        <label>
          Role
          <select
            value={role}
            onChange={(e) => {
              setRole(e.target.value)
              setPage(1)
            }}
          >
            <option value="">Everyone</option>
            <option value="teacher">Teachers</option>
            <option value="student">Parents / Students</option>
            <option value="owner">Owners</option>
          </select>
        </label>
        <label>
          Search by username
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setPage(1)
            }}
            maxLength={100}
          />
        </label>
      </div>

      {!current && <p>Loading…</p>}
      {current?.error && <p className="error">{current.error}</p>}
      {current?.data && results.length === 0 && <p className="empty">No accounts match.</p>}
      {results.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Username</th>
                <th>Name</th>
                <th>Role</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {results.map((user) => (
                <tr key={user.id}>
                  <td>{user.username}</td>
                  <td>{user.first_name || '—'}</td>
                  <td>{ROLE_LABELS[user.role] ?? user.role}</td>
                  <td>
                    <span className={user.is_active ? 'badge present' : 'badge muted'}>
                      {user.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="pager">
        <button onClick={() => setPage(page - 1)} disabled={!current?.data?.previous}>
          Previous
        </button>
        <span>
          Page {page} of {totalPages} · {count} account{count === 1 ? '' : 's'}
        </span>
        <button onClick={() => setPage(page + 1)} disabled={!current?.data?.next}>
          Next
        </button>
      </div>
    </section>
  )
}
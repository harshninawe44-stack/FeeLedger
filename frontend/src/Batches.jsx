import { useEffect, useState } from 'react'
import { api } from './api'
import { formatDate, todayIso } from './dates'

function teacherLabel(user) {
  return user.first_name ? `${user.first_name} (${user.username})` : user.username
}

function BatchForm({ editing, teachers, onSaved, onCancel }) {
  const [name, setName] = useState(editing?.name ?? '')
  const [teacher, setTeacher] = useState(editing ? String(editing.teacher) : '')
  const [startDate, setStartDate] = useState(editing?.start_date ?? todayIso())
  const [isActive, setIsActive] = useState(editing?.is_active ?? true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // A teacher who has left is not in the list. Keep them as an option so the
  // form still shows who the batch has now.
  const options = teachers.map((user) => ({ id: user.id, label: teacherLabel(user) }))
  if (editing && !options.some((option) => option.id === editing.teacher)) {
    options.push({ id: editing.teacher, label: `${editing.teacher_name} (inactive)` })
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    const wanted = {
      name: name.trim(),
      teacher: Number(teacher),
      start_date: startDate,
      is_active: isActive,
    }
    setBusy(true)
    try {
      if (editing) {
        // Send only what changed, so a teacher who has since left is never re-checked.
        const changes = {}
        for (const field of Object.keys(wanted)) {
          if (wanted[field] !== editing[field]) changes[field] = wanted[field]
        }
        if (Object.keys(changes).length === 0) {
          setError('Nothing has changed.')
          return
        }
        await api(`/batches/${editing.id}/`, { method: 'PATCH', body: changes })
        onSaved('Batch updated.')
      } else {
        await api('/batches/', {
          method: 'POST',
          body: {
            name: wanted.name,
            teacher: wanted.teacher,
            start_date: wanted.start_date,
          },
        })
        onSaved('Batch created.')
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="payment-form" onSubmit={handleSubmit}>
      <label>
        Batch name
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={100}
          required
        />
      </label>
      <label>
        Teacher
        <select value={teacher} onChange={(e) => setTeacher(e.target.value)} required>
          <option value="" disabled>
            Choose a teacher…
          </option>
          {options.map((option) => (
            <option key={option.id} value={String(option.id)}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Start date
        <input
          type="date"
          value={startDate}
          onChange={(e) => setStartDate(e.target.value)}
          required
        />
      </label>
      {editing && (
        <label>
          <input
            type="checkbox"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
          />
          Active
        </label>
      )}
      <button type="submit" disabled={busy}>
        {busy ? 'Saving…' : editing ? 'Save changes' : 'Add batch'}
      </button>
      {editing && (
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      )}
      {error && <p className="error full-row">{error}</p>}
    </form>
  )
}

export default function Batches() {
  const [reloadKey, setReloadKey] = useState(0)
  const [batches, setBatches] = useState(null) // { data } or { error }
  const [teachers, setTeachers] = useState(null) // { list } or { error }
  const [editing, setEditing] = useState(null) // a batch being edited, or null
  const [message, setMessage] = useState('')

  useEffect(() => {
    let cancelled = false
    api('/batches/')
      .then((data) => {
        if (!cancelled) setBatches({ data })
      })
      .catch((err) => {
        if (!cancelled) setBatches({ error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  useEffect(() => {
    let cancelled = false
    api('/users/?role=teacher&active=1')
      .then((data) => {
        if (!cancelled) setTeachers({ list: data.results })
      })
      .catch((err) => {
        if (!cancelled) setTeachers({ error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [])

  function handleSaved(text) {
    setMessage(text)
    setEditing(null)
    setReloadKey((n) => n + 1)
  }

  if (!batches || !teachers) return <p>Loading…</p>
  if (batches.error) return <p className="error">{batches.error}</p>
  if (teachers.error) return <p className="error">{teachers.error}</p>

  const rows = batches.data.results

  return (
    <section>
      <h1>Batches</h1>

      <div className="panel">
        <h2>{editing ? `Edit “${editing.name}”` : 'Add a batch'}</h2>
        {teachers.list.length === 0 && (
          <p className="empty">
            There are no active teachers yet. Create a teacher account on the Users tab first.
          </p>
        )}
        {/* The key makes the form start fresh after each save or when another batch is picked. */}
        <BatchForm
          key={`${editing ? editing.id : 'new'}-${reloadKey}`}
          editing={editing}
          teachers={teachers.list}
          onSaved={handleSaved}
          onCancel={() => setEditing(null)}
        />
        {message && <p className="saved">{message}</p>}
      </div>

      <h2>All batches</h2>
      {rows.length === 0 ? (
        <p className="empty">No batches yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Batch</th>
                <th>Teacher</th>
                <th>Start date</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((batch) => (
                <tr key={batch.id}>
                  <td>{batch.name}</td>
                  <td>{batch.teacher_name}</td>
                  <td>{formatDate(batch.start_date)}</td>
                  <td>
                    <span className={batch.is_active ? 'badge present' : 'badge muted'}>
                      {batch.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td>
                    <button
                      onClick={() => {
                        setMessage('')
                        setEditing(batch)
                        window.scrollTo({ top: 0, behavior: 'smooth' })
                      }}
                    >
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {batches.data.next && <p className="empty">Showing the first 50 batches.</p>}
    </section>
  )
}
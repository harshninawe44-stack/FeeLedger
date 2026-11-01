import { useEffect, useState } from 'react'
import { api } from './api'
import { formatDate, todayIso } from './dates'

const PAGE_SIZE = 50 // must match PAGE_SIZE in Django's settings

function parentLabel(user) {
  return user.first_name ? `${user.username} (${user.first_name})` : user.username
}

// Choose a parent account: type to search, then pick from the matches.
function ParentPicker({ value, onChange }) {
  const [search, setSearch] = useState('')
  const [loaded, setLoaded] = useState(null) // { key, list, more } or { key, error }

  useEffect(() => {
    let cancelled = false
    const timer = setTimeout(
      () => {
        const query = encodeURIComponent(search.trim())
        api(`/users/?role=student&active=1&search=${query}`)
          .then((data) => {
            if (!cancelled) {
              setLoaded({ key: search, list: data.results, more: Boolean(data.next) })
            }
          })
          .catch((err) => {
            if (!cancelled) setLoaded({ key: search, error: err.message })
          })
      },
      search ? 300 : 0,
    )
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [search])

  const current = loaded && loaded.key === search ? loaded : null
  const options = [...(current?.list ?? [])]
  // Keep the chosen parent in the list even when a search no longer matches them.
  if (value && !options.some((user) => user.id === value.id)) options.unshift(value)

  return (
    <div className="full-row parent-picker">
      <label>
        Parent account (optional)
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          // Enter must not submit the student form from this box.
          onKeyDown={(e) => {
            if (e.key === 'Enter') e.preventDefault()
          }}
          placeholder="Search by username"
          maxLength={100}
        />
      </label>
      <label>
        Linked parent
        <select
          value={value ? String(value.id) : ''}
          onChange={(e) =>
            onChange(options.find((user) => String(user.id) === e.target.value) ?? null)
          }
        >
          <option value="">{current ? 'No parent linked' : 'Loading…'}</option>
          {options.map((user) => (
            <option key={user.id} value={String(user.id)}>
              {parentLabel(user)}
            </option>
          ))}
        </select>
      </label>
      {current?.error && <p className="error">{current.error}</p>}
      {current?.more && <p className="empty">Showing the first 50 matches. Type to narrow.</p>}
    </div>
  )
}

function StudentForm({ editing, batches, onSaved, onCancel }) {
  const [name, setName] = useState(editing?.full_name ?? '')
  const [phone, setPhone] = useState(editing?.phone ?? '')
  const [guardian, setGuardian] = useState(editing?.guardian_name ?? '')
  const [parent, setParent] = useState(
    editing?.user
      ? { id: editing.user, username: editing.parent_name, first_name: '' }
      : null,
  )
  const [batchId, setBatchId] = useState('')
  const [joinedOn, setJoinedOn] = useState(todayIso())
  const [isActive, setIsActive] = useState(editing?.is_active ?? true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const activeBatches = batches.filter((batch) => batch.is_active)

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (editing) {
        const wanted = {
          full_name: name.trim(),
          phone: phone.trim(),
          guardian_name: guardian.trim(),
          user: parent ? parent.id : null,
          is_active: isActive,
        }
        // Send only what changed, so old data is never re-checked for no reason.
        const changes = {}
        for (const field of Object.keys(wanted)) {
          if (wanted[field] !== editing[field]) changes[field] = wanted[field]
        }
        if (Object.keys(changes).length === 0) {
          setError('Nothing has changed.')
          return
        }
        await api(`/students/${editing.id}/`, { method: 'PATCH', body: changes })
        await onSaved('Student updated.')
      } else {
        const body = {
          full_name: name.trim(),
          phone: phone.trim(),
          guardian_name: guardian.trim(),
        }
        if (parent) body.user = parent.id
        if (batchId) {
          body.batch = Number(batchId)
          body.joined_on = joinedOn
        }
        await api('/students/', { method: 'POST', body })
        await onSaved('Student added.')
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
        Full name
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={150}
          required
        />
      </label>
      <label>
        Phone
        <input
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          inputMode="tel"
          maxLength={20}
          placeholder="9876543210"
          required
        />
      </label>
      <label>
        Guardian (optional)
        <input
          value={guardian}
          onChange={(e) => setGuardian(e.target.value)}
          maxLength={150}
        />
      </label>
      {!editing && (
        <>
          <label>
            Enrol in a batch (optional)
            <select value={batchId} onChange={(e) => setBatchId(e.target.value)}>
              <option value="">Not yet</option>
              {activeBatches.map((batch) => (
                <option key={batch.id} value={String(batch.id)}>
                  {batch.name}
                </option>
              ))}
            </select>
          </label>
          {batchId && (
            <label>
              Joined on
              <input
                type="date"
                value={joinedOn}
                onChange={(e) => setJoinedOn(e.target.value)}
                required
              />
            </label>
          )}
        </>
      )}
      {editing && (
        <label>
          <input
            type="checkbox"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
          />
          Active student
        </label>
      )}
      <ParentPicker value={parent} onChange={setParent} />
      <button type="submit" disabled={busy}>
        {busy ? 'Saving…' : editing ? 'Save changes' : 'Add student'}
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

// The batches one student is in, and a way to add or end an enrolment.
function EnrolPanel({ student, batches, onChanged }) {
  const [batchId, setBatchId] = useState('')
  const [joinedOn, setJoinedOn] = useState(todayIso())
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const taken = new Set(student.enrollments.map((item) => item.batch))
  const available = batches.filter((batch) => batch.is_active && !taken.has(batch.id))

  async function run(action) {
    setError('')
    setBusy(true)
    try {
      await action()
      await onChanged()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function handleEnrol(event) {
    event.preventDefault()
    run(async () => {
      await api('/enrollments/', {
        method: 'POST',
        body: { student: student.id, batch: Number(batchId), joined_on: joinedOn },
      })
      setBatchId('')
    })
  }

  function handleToggle(item) {
    if (
      item.is_active &&
      !window.confirm(`Remove ${student.full_name} from ${item.batch_name}? Their history is kept.`)
    ) {
      return
    }
    run(() =>
      api(`/enrollments/${item.id}/`, {
        method: 'PATCH',
        body: { is_active: !item.is_active },
      }),
    )
  }

  return (
    <div className="panel">
      <h2>Batches for {student.full_name}</h2>
      {student.enrollments.length === 0 ? (
        <p className="empty">Not enrolled in any batch yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Batch</th>
                <th>Joined</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {student.enrollments.map((item) => (
                <tr key={item.id}>
                  <td>{item.batch_name}</td>
                  <td>{formatDate(item.joined_on)}</td>
                  <td>
                    <span className={item.is_active ? 'badge present' : 'badge muted'}>
                      {item.is_active ? 'Enrolled' : 'Left'}
                    </span>
                  </td>
                  <td>
                    <button type="button" onClick={() => handleToggle(item)} disabled={busy}>
                      {item.is_active ? 'Remove from batch' : 'Reactivate'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!student.is_active ? (
        <p className="empty">Reactivate this student before enrolling them in a batch.</p>
      ) : available.length === 0 ? (
        <p className="empty">There is no other active batch to enrol in.</p>
      ) : (
        <form className="inline-form" onSubmit={handleEnrol}>
          <select value={batchId} onChange={(e) => setBatchId(e.target.value)} required>
            <option value="" disabled>
              Choose a batch…
            </option>
            {available.map((batch) => (
              <option key={batch.id} value={String(batch.id)}>
                {batch.name}
              </option>
            ))}
          </select>
          <input
            type="date"
            value={joinedOn}
            onChange={(e) => setJoinedOn(e.target.value)}
            required
          />
          <button type="submit" disabled={busy || !batchId}>
            Enrol
          </button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  )
}

export default function Students() {
  const [search, setSearch] = useState('')
  const [activeOnly, setActiveOnly] = useState(false)
  const [batchFilter, setBatchFilter] = useState('')
  const [noParent, setNoParent] = useState(false)
  const [page, setPage] = useState(1)
  const [reloadKey, setReloadKey] = useState(0)
  const [loaded, setLoaded] = useState(null) // { key, data } or { key, error }
  const [batches, setBatches] = useState(null) // { list } or { error }
  const [editing, setEditing] = useState(null) // the student being edited, or null
  const [formVersion, setFormVersion] = useState(0)
  const [message, setMessage] = useState('')

  useEffect(() => {
    let cancelled = false
    api('/batches/')
      .then((data) => {
        if (!cancelled) setBatches({ list: data.results })
      })
      .catch((err) => {
        if (!cancelled) setBatches({ error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Reload when a filter or the page changes, or after a save. Typing in the
  // search box waits a moment before asking the server.
  useEffect(() => {
    let cancelled = false
    const wanted = `${search}|${activeOnly}|${batchFilter}|${noParent}|${page}`
    const timer = setTimeout(
      () => {
        const query = encodeURIComponent(search.trim())
        let url = `/students/?page=${page}&search=${query}`
        if (activeOnly) url += '&active=1'
        if (batchFilter) url += `&batch=${batchFilter}`
        if (noParent) url += '&without_parent=1'
        api(url)
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
  }, [search, activeOnly, batchFilter, noParent, page, reloadKey])

  // The old list stays on screen while a reload is in progress (no flicker).
  const key = `${search}|${activeOnly}|${batchFilter}|${noParent}|${page}`
  const current = loaded && loaded.key === key ? loaded : null
  const rows = current?.data?.results ?? []
  const count = current?.data?.count ?? 0
  const totalPages = Math.max(1, Math.ceil(count / PAGE_SIZE))

  async function refreshEditing(id) {
    try {
      setEditing(await api(`/students/${id}/`))
    } catch (err) {
      setMessage(err.message)
    }
    setReloadKey((n) => n + 1)
  }

  async function handleSaved(text) {
    setMessage(text)
    setFormVersion((n) => n + 1) // a fresh form, filled from what the server now holds
    if (editing) {
      await refreshEditing(editing.id)
    } else {
      setPage(1)
      setReloadKey((n) => n + 1)
    }
  }

  function startEdit(student) {
    setMessage('')
    setEditing(student)
    setFormVersion((n) => n + 1)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  if (!batches) return <p>Loading…</p>
  if (batches.error) return <p className="error">{batches.error}</p>
  const batchList = batches.list

  return (
    <section>
      <h1>Students</h1>
      <p className="empty">
        Add students one at a time here, or in bulk on the Import students tab. Link a parent
        account so that a parent can see their child’s fees and attendance.
      </p>

      <div className="panel">
        <h2>{editing ? `Edit ${editing.full_name}` : 'Add a student'}</h2>
        <StudentForm
          key={editing ? `edit-${editing.id}-${formVersion}` : `new-${formVersion}`}
          editing={editing}
          batches={batchList}
          onSaved={handleSaved}
          onCancel={() => setEditing(null)}
        />
        {message && <p className="saved">{message}</p>}
      </div>

      {editing && (
        <EnrolPanel
          student={editing}
          batches={batchList}
          onChanged={() => refreshEditing(editing.id)}
        />
      )}

      <h2>All students</h2>
      <div className="toolbar">
        <label>
          Search
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setPage(1)
            }}
            placeholder="Name or phone"
            maxLength={100}
          />
        </label>
        <label>
          Batch
          <select
            value={batchFilter}
            onChange={(e) => {
              setBatchFilter(e.target.value)
              setPage(1)
            }}
          >
            <option value="">All batches</option>
            {batchList.map((batch) => (
              <option key={batch.id} value={String(batch.id)}>
                {batch.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(e) => {
              setActiveOnly(e.target.checked)
              setPage(1)
            }}
          />
          Active students only
        </label>
        <label>
          <input
            type="checkbox"
            checked={noParent}
            onChange={(e) => {
              setNoParent(e.target.checked)
              setPage(1)
            }}
          />
          No parent linked
        </label>
      </div>

      {!current && !loaded && <p>Loading…</p>}
      {loaded?.error && <p className="error">{loaded.error}</p>}
      {current?.data && rows.length === 0 && <p className="empty">No students match.</p>}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Phone</th>
                <th>Guardian</th>
                <th>Parent</th>
                <th>Batches</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((student) => (
                <tr key={student.id}>
                  <td>{student.full_name}</td>
                  <td>{student.phone || '—'}</td>
                  <td>{student.guardian_name || '—'}</td>
                  <td>{student.parent_name ?? '—'}</td>
                  <td>
                    <div className="badge-list">
                      {student.enrollments.length === 0 && '—'}
                      {student.enrollments.map((item) => (
                        <span
                          key={item.id}
                          className={item.is_active ? 'badge present' : 'badge muted'}
                        >
                          {item.batch_name}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td>
                    <span className={student.is_active ? 'badge present' : 'badge muted'}>
                      {student.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td>
                    <button type="button" onClick={() => startEdit(student)}>
                      Edit
                    </button>
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
          Page {page} of {totalPages} · {count} student{count === 1 ? '' : 's'}
        </span>
        <button onClick={() => setPage(page + 1)} disabled={!current?.data?.next}>
          Next
        </button>
      </div>
    </section>
  )
}
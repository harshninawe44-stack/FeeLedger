import { useEffect, useState } from 'react'
import { api } from './api'
import { daysAgoIso, formatDate, todayIso } from './dates'

const STATUSES = [
  ['present', 'Present'],
  ['absent', 'Absent'],
  ['late', 'Late'],
  ['leave', 'Leave'],
]

// Must match TEACHER_EDIT_WINDOW_DAYS in core/services.py. This only greys out
// older dates for teachers. Django enforces the real limit.
const TEACHER_WINDOW_DAYS = 7

export default function Attendance({ role }) {
  const [batches, setBatches] = useState(null) // { list } or { error }
  const [pickedBatch, setPickedBatch] = useState(null)
  const [date, setDate] = useState(todayIso())
  const [loaded, setLoaded] = useState(null) // { key, sheet } or { key, error }
  const [edits, setEdits] = useState({ key: null, marks: {} })
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  // Load the batches this user may see (Django already filters them).
  useEffect(() => {
    let cancelled = false
    api('/batches/')
      .then((data) => {
        if (!cancelled) setBatches({ list: data.results.filter((b) => b.is_active) })
      })
      .catch((err) => {
        if (!cancelled) setBatches({ error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [])

  const list = batches?.list ?? []
  const batchId = pickedBatch ?? list[0]?.id ?? null
  const key = `${batchId}|${date}`

  // Load the roster whenever the batch or the date changes.
  useEffect(() => {
    if (batchId === null || !date) return
    let cancelled = false
    const wanted = `${batchId}|${date}`
    api(`/batches/${batchId}/attendance/?date=${date}`)
      .then((sheet) => {
        if (!cancelled) setLoaded({ key: wanted, sheet })
      })
      .catch((err) => {
        if (!cancelled) setLoaded({ key: wanted, error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [batchId, date])

  const current = loaded && loaded.key === key ? loaded : null
  const records = current?.sheet?.records ?? []

  // What the teacher has changed on this screen but not saved yet.
  const pending = edits.key === key ? edits.marks : {}
  const statusOf = (record) => pending[record.student] ?? record.status
  const unmarked = records.filter((record) => !statusOf(record))
  const dirty = records.some((record) => statusOf(record) !== record.status)

  function setMark(studentId, status) {
    setMessage('')
    setError('')
    setEdits((previous) => ({
      key,
      marks: { ...(previous.key === key ? previous.marks : {}), [studentId]: status },
    }))
  }

  function markAllPresent() {
    setMessage('')
    setError('')
    setEdits({
      key,
      marks: Object.fromEntries(records.map((record) => [record.student, 'present'])),
    })
  }

  async function handleSave() {
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const sheet = await api(`/batches/${batchId}/attendance/`, {
        method: 'POST',
        body: {
          date,
          records: records.map((record) => ({
            student: record.student,
            status: statusOf(record),
          })),
        },
      })
      setLoaded({ key, sheet })
      setEdits({ key: null, marks: {} })
      setMessage('Attendance saved.')
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (!batches) return <p>Loading…</p>
  if (batches.error) return <p className="error">{batches.error}</p>
  if (list.length === 0) return <p className="empty">You have no active batches yet.</p>

  const minDate = role === 'teacher' ? daysAgoIso(TEACHER_WINDOW_DAYS) : undefined

  return (
    <section>
      <h1>Attendance</h1>

      <div className="toolbar">
        <label>
          Batch
          <select
            value={batchId}
            onChange={(e) => {
              setPickedBatch(Number(e.target.value))
              setMessage('')
              setError('')
            }}
          >
            {list.map((batch) => (
              <option key={batch.id} value={batch.id}>
                {batch.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Date
          <input
            type="date"
            value={date}
            min={minDate}
            max={todayIso()}
            onChange={(e) => {
              setDate(e.target.value)
              setMessage('')
              setError('')
            }}
          />
        </label>
        <button type="button" onClick={markAllPresent} disabled={records.length === 0}>
          Mark all present
        </button>
      </div>
      {role === 'teacher' && (
        <p className="empty">
          Teachers can mark or change the last {TEACHER_WINDOW_DAYS} days. For older
          dates, ask the owner.
        </p>
      )}

      {!date && <p className="empty">Choose a date.</p>}
      {date && !current && <p>Loading…</p>}
      {current?.error && <p className="error">{current.error}</p>}

      {current?.sheet && records.length === 0 && (
        <p className="empty">No students are enrolled in this batch.</p>
      )}

      {records.length > 0 && (
        <>
          <p className="summary">
            {formatDate(date)} ·{' '}
            {STATUSES.map(([value, label]) => {
              const count = records.filter((r) => statusOf(r) === value).length
              return `${label} ${count}`
            }).join(' · ')}{' '}
            · Unmarked {unmarked.length}
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Student</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {records.map((record) => {
                  const status = statusOf(record)
                  return (
                    <tr key={record.student} className={status ? '' : 'unmarked'}>
                      <td>{record.student_name}</td>
                      <td>
                        <div
                          className="segmented"
                          role="group"
                          aria-label={`Attendance for ${record.student_name}`}
                        >
                          {STATUSES.map(([value, label]) => (
                            <button
                              key={value}
                              type="button"
                              className={`seg ${value}${status === value ? ' on' : ''}`}
                              aria-pressed={status === value}
                              onClick={() => setMark(record.student, value)}
                            >
                              {label}
                            </button>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <div className="save-bar">
            <button
              type="button"
              className="primary"
              onClick={handleSave}
              disabled={saving || unmarked.length > 0 || !dirty}
            >
              {saving ? 'Saving…' : 'Save attendance'}
            </button>
            {unmarked.length > 0 && (
              <span className="empty">
                {unmarked.length} student{unmarked.length === 1 ? '' : 's'} still unmarked
              </span>
            )}
            {unmarked.length === 0 && dirty && <span className="empty">Unsaved changes</span>}
            {message && <span className="saved">{message}</span>}
          </div>
          {error && <p className="error">{error}</p>}
        </>
      )}
    </section>
  )
}
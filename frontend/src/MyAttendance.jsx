import { useEffect, useState } from 'react'
import { api } from './api'
import { formatDate } from './dates'

const STATUS_LABELS = {
  present: 'Present',
  absent: 'Absent',
  late: 'Late',
  leave: 'Leave',
}
const PAGE_SIZE = 50 // must match PAGE_SIZE in Django's settings
const LOW_ATTENDANCE = 75 // a card turns red below this percentage

export default function MyAttendance() {
  const [summary, setSummary] = useState(null) // { rows } or { error }
  const [studentId, setStudentId] = useState('') // '' means every child
  const [page, setPage] = useState(1)
  const [loaded, setLoaded] = useState(null) // { key, data } or { key, error }

  // Totals per child and batch. One request, computed by the database.
  useEffect(() => {
    let cancelled = false
    api('/attendance/summary/')
      .then((rows) => {
        if (!cancelled) setSummary({ rows })
      })
      .catch((err) => {
        if (!cancelled) setSummary({ error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [])

  // The day-by-day list, one page at a time.
  useEffect(() => {
    let cancelled = false
    const key = `${studentId}|${page}`
    const filter = studentId ? `&student=${studentId}` : ''
    api(`/attendance/?page=${page}${filter}`)
      .then((data) => {
        if (!cancelled) setLoaded({ key, data })
      })
      .catch((err) => {
        if (!cancelled) setLoaded({ key, error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [studentId, page])

  if (!summary) return <p>Loading…</p>
  if (summary.error) return <p className="error">{summary.error}</p>

  const rows = summary.rows
  if (rows.length === 0) {
    return (
      <section>
        <h1>Attendance</h1>
        <p className="empty">No attendance has been marked yet.</p>
      </section>
    )
  }

  // The children who have attendance, for the filter box.
  const children = []
  for (const row of rows) {
    if (!children.some((child) => child.id === row.student)) {
      children.push({ id: row.student, name: row.student_name })
    }
  }
  const shown = studentId ? rows.filter((row) => String(row.student) === studentId) : rows

  const current = loaded && loaded.key === `${studentId}|${page}` ? loaded : null
  const results = current?.data?.results ?? []
  const count = current?.data?.count ?? 0
  const totalPages = Math.max(1, Math.ceil(count / PAGE_SIZE))

  return (
    <section>
      <h1>Attendance</h1>

      {children.length > 1 && (
        <div className="toolbar">
          <label>
            Child
            <select
              value={studentId}
              onChange={(e) => {
                setStudentId(e.target.value)
                setPage(1)
              }}
            >
              <option value="">All children</option>
              {children.map((child) => (
                <option key={child.id} value={String(child.id)}>
                  {child.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      <div className="cards">
        {shown.map((row) => {
          const low = row.percent !== null && row.percent < LOW_ATTENDANCE
          return (
            <div key={`${row.student}-${row.batch}`} className={low ? 'card low' : 'card'}>
              <h3>{row.student_name}</h3>
              <small>{row.batch_name}</small>
              <div className="big">{row.percent === null ? '—' : `${row.percent}%`}</div>
              <small>
                Present {row.present} · Absent {row.absent} · Late {row.late} · Leave{' '}
                {row.leave}
              </small>
              {low && <small>Below {LOW_ATTENDANCE}%</small>}
            </div>
          )
        })}
      </div>
      <p className="empty">
        Attendance % = days present or late, out of all days that were not marked Leave.
      </p>

      <h2>Recent days</h2>
      {!current && <p>Loading…</p>}
      {current?.error && <p className="error">{current.error}</p>}
      {current?.data && (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Student</th>
                  <th>Batch</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {results.map((record) => (
                  <tr key={record.id}>
                    <td>{formatDate(record.date)}</td>
                    <td>{record.student_name}</td>
                    <td>{record.batch_name}</td>
                    <td>
                      <span className={`badge ${record.status}`}>
                        {STATUS_LABELS[record.status] ?? record.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <button onClick={() => setPage(page - 1)} disabled={!current.data.previous}>
              Previous
            </button>
            <span>
              Page {page} of {totalPages} · {count} record{count === 1 ? '' : 's'}
            </span>
            <button onClick={() => setPage(page + 1)} disabled={!current.data.next}>
              Next
            </button>
          </div>
        </>
      )}
    </section>
  )
}
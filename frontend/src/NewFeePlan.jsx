import { useEffect, useState } from 'react'
import { api } from './api'
import { addMonthsIso, formatDate, todayIso } from './dates'
import { formatRupees } from './money'

const MAX_INSTALLMENTS = 24 // must match max_length for due_dates in Django

export default function NewFeePlan({ onCreated }) {
  const [search, setSearch] = useState('')
  const [loaded, setLoaded] = useState(null) // { key, data } or { key, error }
  const [pickedId, setPickedId] = useState(null)
  const [total, setTotal] = useState('')
  const [dates, setDates] = useState([''])
  const [count, setCount] = useState('4')
  const [first, setFirst] = useState(todayIso())
  const [preview, setPreview] = useState(null) // { key, rows }
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Load the students who still need a fee plan. Waits a moment while typing.
  useEffect(() => {
    let cancelled = false
    const timer = setTimeout(
      () => {
        const query = encodeURIComponent(search.trim())
        api(`/enrollments/?without_plan=1&active=1&search=${query}`)
          .then((data) => {
            if (!cancelled) setLoaded({ key: search, data })
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
  const list = current?.data?.results ?? []
  const selected = list.find((item) => item.id === pickedId) ?? null

  const ready =
    selected !== null && total.trim() !== '' && dates.length > 0 && dates.every(Boolean)

  // The preview is only valid for exactly the inputs it was made from.
  const inputKey = JSON.stringify([selected?.id ?? null, total.trim(), dates])
  const fresh = preview !== null && preview.key === inputKey

  function setDateAt(index, value) {
    setDates(dates.map((date, i) => (i === index ? value : date)))
  }

  function removeDateAt(index) {
    setDates(dates.filter((_, i) => i !== index))
  }

  function addDate() {
    if (dates.length < MAX_INSTALLMENTS) setDates([...dates, ''])
  }

  function fillMonthly() {
    const n = Number(count)
    if (!Number.isInteger(n) || n < 1 || n > MAX_INSTALLMENTS || !first) {
      setError(`Choose a first date and a number of installments from 1 to ${MAX_INSTALLMENTS}.`)
      return
    }
    setError('')
    setDates(Array.from({ length: n }, (_, i) => addMonthsIso(first, i)))
  }

  async function handlePreview() {
    setError('')
    setBusy(true)
    try {
      const data = await api('/fee-plans/preview/', {
        method: 'POST',
        body: { total_amount: total.trim(), due_dates: dates },
      })
      setPreview({ key: inputKey, rows: data.installments })
    } catch (err) {
      setPreview(null)
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleCreate() {
    setError('')
    setBusy(true)
    try {
      const plan = await api('/fee-plans/', {
        method: 'POST',
        body: { enrollment: selected.id, total_amount: total.trim(), due_dates: dates },
      })
      onCreated(plan.id) // opens the new plan, so this screen goes away
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <h1>New fee plan</h1>
      <p className="empty">
        Pick a student who has no fee plan yet, enter the total, and choose the due dates.
        The server splits the total so the installments add up exactly.
      </p>

      <div className="panel">
        <h2>1. Student</h2>
        <div className="toolbar">
          <label>
            Search by name
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Type part of a name"
              maxLength={100}
            />
          </label>
          <label>
            Student
            <select
              value={selected?.id ?? ''}
              onChange={(e) => setPickedId(Number(e.target.value))}
            >
              <option value="" disabled>
                {current ? 'Choose a student…' : 'Loading…'}
              </option>
              {list.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.student_name} — {item.batch_name}
                </option>
              ))}
            </select>
          </label>
        </div>
        {current?.error && <p className="error">{current.error}</p>}
        {current && !current.error && list.length === 0 && (
          <p className="empty">No students without a fee plan match.</p>
        )}
        {current?.data?.next && (
          <p className="empty">Showing the first 50. Type a name to narrow the list.</p>
        )}
      </div>

      <div className="panel">
        <h2>2. Total fee</h2>
        <label>
          Amount (₹){' '}
          <input
            inputMode="decimal"
            value={total}
            onChange={(e) => setTotal(e.target.value)}
            placeholder="60000.00"
            pattern="[0-9]+(\.[0-9]{1,2})?"
            title="An amount like 60000 or 60000.50"
          />
        </label>
      </div>

      <div className="panel">
        <h2>3. Due dates</h2>
        <div className="toolbar">
          <label>
            Number of installments
            <input
              inputMode="numeric"
              value={count}
              onChange={(e) => setCount(e.target.value)}
              size={4}
            />
          </label>
          <label>
            First due date
            <input type="date" value={first} onChange={(e) => setFirst(e.target.value)} />
          </label>
          <button type="button" onClick={fillMonthly}>
            Fill monthly dates
          </button>
        </div>
        {dates.map((value, index) => (
          <div className="field-row" key={index}>
            <span>{index + 1}.</span>
            <input
              type="date"
              value={value}
              onChange={(e) => setDateAt(index, e.target.value)}
            />
            <button
              type="button"
              onClick={() => removeDateAt(index)}
              disabled={dates.length === 1}
            >
              Remove
            </button>
          </div>
        ))}
        <button type="button" onClick={addDate} disabled={dates.length >= MAX_INSTALLMENTS}>
          Add a date
        </button>
      </div>

      <div className="panel">
        <h2>4. Check and create</h2>
        <div className="save-bar">
          <button type="button" onClick={handlePreview} disabled={!ready || busy}>
            Preview split
          </button>
          <button type="button" className="primary" onClick={handleCreate} disabled={!fresh || busy}>
            Create fee plan
          </button>
        </div>
        {error && <p className="error">{error}</p>}
        {preview && !fresh && (
          <p className="empty">You changed something. Preview again before creating.</p>
        )}
        {fresh && (
          <>
            <p className="summary">
              {selected.student_name} · {formatRupees(total.trim())} in {preview.rows.length}{' '}
              installment{preview.rows.length === 1 ? '' : 's'}
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Due date</th>
                    <th className="num">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row) => (
                    <tr key={row.number}>
                      <td>{row.number}</td>
                      <td>{formatDate(row.due_date)}</td>
                      <td className="num">{formatRupees(row.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </section>
  )
}
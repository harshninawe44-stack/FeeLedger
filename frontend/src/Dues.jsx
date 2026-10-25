import { useEffect, useState } from 'react'
import { api } from './api'
import { formatRupees, isZeroMoney } from './money'

const PAGE_SIZE = 50 // must match PAGE_SIZE in Django's settings

export default function Dues({ role }) {
  const [page, setPage] = useState(1)
  // Holds the answer for one page: { page, result } or { page, error }.
  const [loaded, setLoaded] = useState(null)

  useEffect(() => {
    let cancelled = false
    api(`/dues/?page=${page}`)
      .then((result) => {
        if (!cancelled) setLoaded({ page, result })
      })
      .catch((err) => {
        if (!cancelled) setLoaded({ page, error: err.message })
      })
    // If the page changes before the answer arrives, ignore the old answer.
    return () => {
      cancelled = true
    }
  }, [page])

  // "Loading" simply means: we do not yet have the answer for this page.
  const current = loaded && loaded.page === page ? loaded : null
  const title = role === 'student' ? 'My fees' : 'Fee dues'

  if (!current) return <p>Loading…</p>
  if (current.error) return <p className="error">{current.error}</p>

  const { count, results, previous, next } = current.result
  const totalPages = Math.max(1, Math.ceil(count / PAGE_SIZE))

  return (
    <section>
      <h1>{title}</h1>
      {results.length === 0 ? (
        <p className="empty">No fee plans to show yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Student</th>
                <th>Batch</th>
                <th className="num">Total</th>
                <th className="num">Paid</th>
                <th className="num">Due so far</th>
                <th className="num">Overdue</th>
                <th className="num">Balance</th>
              </tr>
            </thead>
            <tbody>
              {results.map((row) => (
                <tr key={row.fee_plan_id}>
                  <td>{row.student}</td>
                  <td>{row.batch}</td>
                  <td className="num">{formatRupees(row.total_amount)}</td>
                  <td className="num">{formatRupees(row.paid)}</td>
                  <td className="num">{formatRupees(row.due_so_far)}</td>
                  <td className={isZeroMoney(row.overdue) ? 'num' : 'num overdue'}>
                    {formatRupees(row.overdue)}
                  </td>
                  <td className="num">{formatRupees(row.balance)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="pager">
        <button onClick={() => setPage(page - 1)} disabled={!previous}>
          Previous
        </button>
        <span>
          Page {page} of {totalPages} · {count} fee plan{count === 1 ? '' : 's'}
        </span>
        <button onClick={() => setPage(page + 1)} disabled={!next}>
          Next
        </button>
      </div>
    </section>
  )
}
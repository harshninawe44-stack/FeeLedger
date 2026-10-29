import { Fragment, useEffect, useState } from 'react'
import { api } from './api'
import { formatDate, todayIso } from './dates'
import { formatRupees, isZeroMoney } from './money'

const METHODS = [
  ['cash', 'Cash'],
  ['upi', 'UPI'],
  ['cheque', 'Cheque'],
  ['bank', 'Bank transfer'],
]
const METHOD_LABELS = Object.fromEntries(METHODS)

function PaymentForm({ planId, onSaved }) {
  const [amount, setAmount] = useState('')
  const [method, setMethod] = useState('cash')
  const [paidOn, setPaidOn] = useState(todayIso())
  const [reference, setReference] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      await api('/payments/', {
        method: 'POST',
        body: {
          fee_plan: planId,
          amount: amount.trim(), // sent as text, never as a JavaScript number
          method,
          paid_on: paidOn,
          reference: reference.trim(),
          note: note.trim(),
        },
      })
      setAmount('')
      setReference('')
      setNote('')
      onSaved()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="payment-form" onSubmit={handleSubmit}>
      <label>
        Amount (₹)
        <input
          inputMode="decimal"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          placeholder="15000.00"
          pattern="[0-9]+(\.[0-9]{1,2})?"
          title="An amount like 15000 or 15000.50"
          required
        />
      </label>
      <label>
        Method
        <select value={method} onChange={(e) => setMethod(e.target.value)}>
          {METHODS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Paid on
        <input
          type="date"
          value={paidOn}
          max={todayIso()}
          onChange={(e) => setPaidOn(e.target.value)}
          required
        />
      </label>
      <label>
        Reference
        <input
          value={reference}
          onChange={(e) => setReference(e.target.value)}
          placeholder="UPI id, cheque no."
          maxLength={100}
        />
      </label>
      <label>
        Note
        <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={255} />
      </label>
      <button type="submit" disabled={busy}>
        {busy ? 'Saving…' : 'Record payment'}
      </button>
      {error && <p className="error full-row">{error}</p>}
    </form>
  )
}

export default function PlanDetail({ planId, role, onBack }) {
  const [reloadKey, setReloadKey] = useState(0)
  const [loaded, setLoaded] = useState(null)
  const [reversingId, setReversingId] = useState(null)
  const [reason, setReason] = useState('')
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      api(`/fee-plans/${planId}/`),
      api(`/dues/?fee_plan=${planId}`),
      api(`/payments/?fee_plan=${planId}`),
    ])
      .then(([plan, dues, payments]) => {
        if (!cancelled) {
          setLoaded({ planId, plan, summary: dues.results[0] ?? null, payments })
        }
      })
      .catch((err) => {
        if (!cancelled) setLoaded({ planId, error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [planId, reloadKey])

  // While reloading after a change, the old data stays on screen (no flicker).
  const current = loaded && loaded.planId === planId ? loaded : null
  const isOwner = role === 'owner'
  const refresh = () => setReloadKey((n) => n + 1)

  async function handleReverse(event) {
    event.preventDefault()
    setActionError('')
    setBusy(true)
    try {
      await api(`/payments/${reversingId}/reverse/`, {
        method: 'POST',
        body: { reason: reason.trim() },
      })
      setReversingId(null)
      setReason('')
      refresh()
    } catch (err) {
      setActionError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const backButton = (
    <button className="back" onClick={onBack}>
      ← Back to fees
    </button>
  )

  if (!current) return <p>Loading…</p>
  if (current.error) {
    return (
      <section>
        {backButton}
        <p className="error">{current.error}</p>
      </section>
    )
  }

  const { plan, summary, payments } = current
  const entries = payments.results
  const today = todayIso()
  const columnCount = isOwner ? 7 : 6

  return (
    <section>
      {backButton}
      <h1>
        {plan.student} <small>· {plan.batch}</small>
      </h1>

      {summary && (
        <div className="stats">
          {[
            ['Total', summary.total_amount],
            ['Paid', summary.paid],
            ['Due so far', summary.due_so_far],
            ['Overdue', summary.overdue],
            ['Balance', summary.balance],
          ].map(([label, value]) => (
            <div
              key={label}
              className={label === 'Overdue' && !isZeroMoney(value) ? 'stat overdue' : 'stat'}
            >
              <span>{label}</span>
              <strong>{formatRupees(value)}</strong>
            </div>
          ))}
        </div>
      )}

      <h2>Installments</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Due date</th>
              <th className="num">Amount</th>
              <th>Date passed?</th>
            </tr>
          </thead>
          <tbody>
            {plan.installments.map((item) => (
              <tr key={item.number}>
                <td>{item.number}</td>
                <td>{formatDate(item.due_date)}</td>
                <td className="num">{formatRupees(item.amount)}</td>
                <td>{item.due_date <= today ? 'Yes' : 'Upcoming'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="empty">
        Payments are not matched to single installments. The overdue figure above
        shows how much is late.
      </p>

      {isOwner && (
        <>
          <h2>Record a payment</h2>
          <PaymentForm planId={planId} onSaved={refresh} />
        </>
      )}

      <h2>Payment history</h2>
      {actionError && <p className="error">{actionError}</p>}
      {entries.length === 0 ? (
        <p className="empty">No payments recorded yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Entry</th>
                <th>Method</th>
                <th className="num">Amount</th>
                <th>Details</th>
                <th>Recorded by</th>
                {isOwner && <th></th>}
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const isPayment = entry.kind === 'payment'
                const cancelled = isPayment && entry.is_reversed
                return (
                  <Fragment key={entry.id}>
                    <tr className={cancelled ? 'cancelled' : ''}>
                      <td>{formatDate(entry.paid_on)}</td>
                      <td>
                        {isPayment
                          ? `Payment #${entry.id}${cancelled ? ' (reversed)' : ''}`
                          : `Reversal of #${entry.reverses}`}
                      </td>
                      <td>{METHOD_LABELS[entry.method] ?? entry.method}</td>
                      <td className="num">{formatRupees(entry.amount)}</td>
                      <td>{[entry.reference, entry.note].filter(Boolean).join(' · ')}</td>
                      <td>{entry.received_by_name}</td>
                      {isOwner && (
                        <td>
                          {isPayment && !cancelled && (
                            <button
                              onClick={() => {
                                setReversingId(entry.id)
                                setReason('')
                                setActionError('')
                              }}
                            >
                              Reverse
                            </button>
                          )}
                        </td>
                      )}
                    </tr>
                    {reversingId === entry.id && (
                      <tr>
                        <td colSpan={columnCount}>
                          <form className="inline-form" onSubmit={handleReverse}>
                            <input
                              value={reason}
                              onChange={(e) => setReason(e.target.value)}
                              placeholder="Reason for reversing (required)"
                              maxLength={255}
                              required
                            />
                            <button type="submit" disabled={busy}>
                              {busy ? 'Reversing…' : 'Confirm reversal'}
                            </button>
                            <button type="button" onClick={() => setReversingId(null)}>
                              Cancel
                            </button>
                          </form>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {payments.next && <p className="empty">Showing the 50 most recent entries.</p>}
    </section>
  )
}
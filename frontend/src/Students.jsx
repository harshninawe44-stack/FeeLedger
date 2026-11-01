import { useState } from 'react'
import { api, ApiError } from './api'

const MAX_BYTES = 1_000_000 // must match MAX_BYTES in core/importers.py

const TEMPLATE =
  'full_name,phone,guardian_name,batch,joined_on\n' +
  'Asha Rao,9876543210,Mr Rao,JEE Morning,2026-06-01\n'

function downloadTemplate() {
  const url = URL.createObjectURL(new Blob([TEMPLATE], { type: 'text/csv' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'students-template.csv'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export default function ImportStudents() {
  const [file, setFile] = useState(null)
  const [outcome, setOutcome] = useState(null) // what to show under the buttons
  const [busy, setBusy] = useState(false)
  const [inputKey, setInputKey] = useState(0) // changing this empties the file box

  function handleFile(event) {
    const chosen = event.target.files[0] ?? null
    setOutcome(null)
    if (chosen && chosen.size > MAX_BYTES) {
      setFile(null)
      setOutcome({ kind: 'problem', message: 'The file is too large (the limit is 1 MB).' })
      return
    }
    setFile(chosen)
  }

  async function send(commit) {
    setBusy(true)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('commit', commit ? 'true' : 'false')
      const result = await api('/imports/students/', { method: 'POST', body: form })
      if (commit) {
        setOutcome({ kind: 'saved', result })
        setFile(null)
        setInputKey((key) => key + 1)
      } else {
        setOutcome({ kind: 'preview', result })
      }
    } catch (err) {
      const errors = err instanceof ApiError ? err.data?.errors : null
      if (Array.isArray(errors) && errors.length > 0) {
        setOutcome({ kind: 'rows', errors })
      } else {
        setOutcome({ kind: 'problem', message: err.message })
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <h1>Import students</h1>
      <p className="empty">
        Upload a CSV file (in Excel, save as “CSV UTF-8”). Required columns:{' '}
        <strong>full_name</strong>, <strong>phone</strong>, <strong>batch</strong>. Optional:{' '}
        <strong>guardian_name</strong>, <strong>joined_on</strong> (like 2026-06-01). The batch
        must already exist. Nothing is saved until the file passes the check and you click Import.
      </p>

      <div className="panel">
        <div className="toolbar">
          <button type="button" onClick={downloadTemplate}>
            Download template
          </button>
          <input key={inputKey} type="file" accept=".csv,text/csv" onChange={handleFile} />
          <button type="button" onClick={() => send(false)} disabled={!file || busy}>
            {busy ? 'Working…' : 'Check file'}
          </button>
          <button
            type="button"
            className="primary"
            onClick={() => send(true)}
            disabled={outcome?.kind !== 'preview' || !file || busy}
          >
            Import
          </button>
        </div>
      </div>

      {outcome?.kind === 'problem' && <p className="error">{outcome.message}</p>}

      {outcome?.kind === 'preview' && (
        <div className="notice">
          <strong>The file is valid.</strong> {outcome.result.rows} row(s):{' '}
          {outcome.result.new_students} new student(s),{' '}
          {outcome.result.existing_students_matched} existing student(s) matched,{' '}
          {outcome.result.new_enrollments} enrollment(s) to create. Nothing has been saved yet.
          Click <em>Import</em> to save.
        </div>
      )}

      {outcome?.kind === 'saved' && (
        <div className="notice">
          <strong>Import complete.</strong> {outcome.result.new_enrollments} enrollment(s)
          created, including {outcome.result.new_students} new student(s). They now appear in
          the New fee plan list.
        </div>
      )}

      {outcome?.kind === 'rows' && (
        <>
          <p className="error">
            Nothing was saved. {outcome.errors.length} problem(s) found. Fix them in your
            spreadsheet and check the file again. Line 1 is the header row, so line numbers
            match the row numbers in Excel.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Line</th>
                  <th>Column</th>
                  <th>Problem</th>
                </tr>
              </thead>
              <tbody>
                {outcome.errors.map((item, index) => (
                  <tr key={index}>
                    <td>{item.row}</td>
                    <td>{item.field}</td>
                    <td>{item.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}
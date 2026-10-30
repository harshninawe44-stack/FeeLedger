// Dates arrive from Django as text like "2026-10-26". We only rearrange the
// text, so there are no time-zone surprises.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function toIso(date) {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

export function todayIso() {
  return toIso(new Date())
}

export function daysAgoIso(days) {
  const date = new Date()
  date.setDate(date.getDate() - days)
  return toIso(date)
}

// "2026-01-31" + 1 month -> "2026-02-28" (the day is clamped to the month's end)
export function addMonthsIso(iso, months) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? '')
  if (!match) return ''
  const monthIndex = Number(match[2]) - 1 + months
  const year = Number(match[1]) + Math.floor(monthIndex / 12)
  const month = ((monthIndex % 12) + 12) % 12
  const lastDay = new Date(year, month + 1, 0).getDate()
  return toIso(new Date(year, month, Math.min(Number(match[3]), lastDay)))
}

export function formatDate(iso) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? '')
  if (!match) return '—'
  const [, year, month, day] = match
  return `${Number(day)} ${MONTHS[Number(month) - 1]} ${year}`
}
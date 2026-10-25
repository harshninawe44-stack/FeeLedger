// Money arrives from Django as text like "60000.00". We never turn it into a
// JavaScript number (floats are inexact). We only rearrange the text.

const MONEY_PATTERN = /^-?\d+(\.\d+)?$/

export function formatRupees(value) {
  if (typeof value !== 'string' || !MONEY_PATTERN.test(value)) return '—'
  const negative = value.startsWith('-')
  const [whole, fraction = '00'] = value.replace('-', '').split('.')

  // Indian grouping: the last three digits, then pairs. 6000000 -> 60,00,000
  const last3 = whole.slice(-3)
  const rest = whole.slice(0, -3)
  const grouped = rest
    ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + last3
    : last3

  return `${negative ? '-' : ''}₹${grouped}.${fraction.padEnd(2, '0')}`
}

export function isZeroMoney(value) {
  return typeof value === 'string' && /^-?0+(\.0+)?$/.test(value)
}
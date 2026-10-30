// Talks to the Django API. Every request goes to /api/... on the same address
// as the page, so the browser sends the login cookie automatically.

export class ApiError extends Error {
  constructor(status, message, data = null) {
    super(message)
    this.status = status
    this.data = data // the raw error body, for screens that need more detail
  }
}

function getCookie(name) {
  const prefix = name + '='
  const found = document.cookie.split('; ').find((part) => part.startsWith(prefix))
  return found ? decodeURIComponent(found.slice(prefix.length)) : null
}

// Django errors arrive in a few shapes (text, lists, nested objects).
// Collect every piece of text into one list.
function flatten(value) {
  if (typeof value === 'string') return [value]
  if (Array.isArray(value)) return value.flatMap(flatten)
  if (value && typeof value === 'object') return Object.values(value).flatMap(flatten)
  return []
}

function messageFrom(data) {
  const source = data && typeof data === 'object' && 'detail' in data ? data.detail : data
  const parts = flatten(source)
  return parts.length ? parts.join(' ') : 'Something went wrong.'
}

export async function api(path, { method = 'GET', body } = {}) {
  const headers = { Accept: 'application/json' }
  const isForm = body instanceof FormData
  // A file upload sets its own Content-Type (with a boundary), so we leave it out.
  if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json'

  // Anything that changes data must carry the CSRF token Django gave us.
  if (method !== 'GET') {
    const token = getCookie('csrftoken')
    if (token) headers['X-CSRFToken'] = token
  }

  let response
  try {
    response = await fetch('/api' + path, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'Cannot reach the server. Is it running?')
  }

  if (response.status === 204) return null // "done, nothing to return"

  let data = null
  try {
    data = await response.json()
  } catch {
    // the response had no JSON body
  }
  if (!response.ok) throw new ApiError(response.status, messageFrom(data), data)
  return data
}
// Talks to the Django API. Every request goes to /api/... on the same address
// as the page, so the browser sends the login cookie automatically.

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

function getCookie(name) {
  const prefix = name + '='
  const found = document.cookie.split('; ').find((part) => part.startsWith(prefix))
  return found ? decodeURIComponent(found.slice(prefix.length)) : null
}

// Django errors arrive in a few shapes. Turn any of them into one message.
function messageFrom(data) {
  if (!data || typeof data !== 'object') return 'Something went wrong.'
  const detail = data.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.join(' ')
  const first = Object.values(data)[0]
  if (Array.isArray(first)) return first.join(' ')
  if (typeof first === 'string') return first
  return 'Something went wrong.'
}

export async function api(path, { method = 'GET', body } = {}) {
  const headers = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

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
      body: body === undefined ? undefined : JSON.stringify(body),
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
  if (!response.ok) throw new ApiError(response.status, messageFrom(data))
  return data
}
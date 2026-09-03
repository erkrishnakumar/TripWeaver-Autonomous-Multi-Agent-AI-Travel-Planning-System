import axios, { AxiosError } from 'axios'
import type {
  ApiErrorBody,
  ApprovalDecisionResponse,
  AuditLogEntryRead,
  BookingRead,
  ComponentClientKeyResponse,
  ConfirmApprovalRequest,
  ConfirmInfoResponse,
  RejectApprovalRequest,
  TokenResponse,
  TripCreate,
  TripProceedResponse,
  TripRead,
  UserRead,
} from '@/lib/types'

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

// Deliberately in-memory only, never persisted to localStorage/sessionStorage
// -- a page refresh should always require logging back in, not silently
// resume a prior session.
let inMemoryToken: string | null = null

export const authStorage = {
  get: () => inMemoryToken,
  set: (token: string) => {
    inMemoryToken = token
  },
  clear: () => {
    inMemoryToken = null
  },
}

export const api = axios.create({ baseURL: API_BASE_URL })

api.interceptors.request.use((config) => {
  const token = authStorage.get()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      authStorage.clear()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  },
)

export function extractErrorMessage(error: unknown, fallback = 'Something went wrong.'): string {
  if (axios.isAxiosError(error)) {
    const body = error.response?.data as ApiErrorBody | undefined
    if (typeof body?.detail === 'string') return body.detail
    if (Array.isArray(body?.detail) && body.detail.length > 0) return body.detail[0].msg
    if (error.message) return error.message
  }
  return fallback
}

export const authApi = {
  register: (email: string, password: string, full_name?: string) =>
    api.post<UserRead>('/auth/register', { email, password, full_name: full_name || null }).then((r) => r.data),
  login: (email: string, password: string) =>
    api.post<TokenResponse>('/auth/login', { email, password }).then((r) => r.data),
  me: () => api.get<UserRead>('/auth/me').then((r) => r.data),
  updateMe: (full_name: string | null) =>
    api.patch<UserRead>('/auth/me', { full_name }).then((r) => r.data),
  forgotPassword: (email: string) =>
    api.post<{ message: string; reset_token?: string | null }>('/auth/forgot-password', { email }).then((r) => r.data),
  resetPassword: (token: string, new_password: string) =>
    api.post<UserRead>('/auth/reset-password', { token, new_password }).then((r) => r.data),
}

export const tripsApi = {
  get: (tripId: string) => api.get<TripRead>(`/trips/${tripId}`).then((r) => r.data),
  create: (body: TripCreate) => api.post<TripRead>('/trips', body).then((r) => r.data),
  proceed: (tripId: string) =>
    api.post<TripProceedResponse>(`/trips/${tripId}/proceed`).then((r) => r.data),
  listBookings: (tripId: string) =>
    api.get<BookingRead[]>(`/trips/${tripId}/bookings`).then((r) => r.data),
  getBookingConfirmInfo: (tripId: string, bookingId: string) =>
    api
      .get<ConfirmInfoResponse>(`/trips/${tripId}/bookings/${bookingId}/confirm-info`)
      .then((r) => r.data),
  getAuditLog: (tripId: string) =>
    api.get<AuditLogEntryRead[]>(`/trips/${tripId}/audit-log`).then((r) => r.data),
}

export const approvalsApi = {
  confirm: (approvalId: string, body: ConfirmApprovalRequest) =>
    api
      .post<ApprovalDecisionResponse>(`/approvals/${approvalId}/confirm`, body)
      .then((r) => r.data),
  reject: (approvalId: string, body: RejectApprovalRequest) =>
    api.post<ApprovalDecisionResponse>(`/approvals/${approvalId}/reject`, body).then((r) => r.data),
}

export const carRentalsApi = {
  getComponentClientKey: () =>
    api
      .get<ComponentClientKeyResponse>('/car-rentals/component-client-key')
      .then((r) => r.data.component_client_key),
}

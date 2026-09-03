export type TripStatus =
  | 'draft'
  | 'researching'
  | 'planning'
  | 'awaiting_approval'
  | 'approved'
  | 'booked'
  | 'cancelled'
  | 'failed'

export type BookingType = 'flight' | 'hotel' | 'car'

export type BookingStatus =
  | 'pending_approval'
  | 'approved'
  | 'rejected'
  | 'booked'
  | 'booking_failed'
  | 'cancelled'

export type ApprovalDecision = 'pending' | 'approved' | 'rejected'

export interface TripRead {
  id: string
  origin_iata: string
  destination_iata: string
  depart_date: string
  return_date: string | null
  adults: number
  max_budget_usd: number | null
  status: TripStatus
  created_at: string
  updated_at: string
}

export interface TripCreate {
  origin_iata: string
  destination_iata: string
  depart_date: string
  return_date?: string | null
  adults?: number
  max_budget_usd?: number | null
  requester_email?: string | null
  wants_car_rental?: boolean
}

export interface TripProceedResponse {
  trip_id: string
  status: string
  message: string
}

export interface BookingRead {
  booking_id: string
  booking_type: BookingType
  status: BookingStatus
  total_price_usd: number
  provider_booking_reference: string | null
  failure_reason: string | null
  approval_id: string
  approval_decision: ApprovalDecision
}

export interface ConfirmInfoResponse {
  booking_type: BookingType
  approval_id: string
  passenger_ids: string[] | null
  car_quote_id: string | null
  note: string
}

export interface ComponentClientKeyResponse {
  component_client_key: string
}

export interface AuditLogEntryRead {
  sequence: number
  event_type: string
  payload: Record<string, unknown>
  booking_id: string | null
  created_at: string
}

export type PassengerTitle = 'mr' | 'mrs' | 'ms' | 'miss'
export type PassengerGender = 'm' | 'f'

export interface PassengerDetails {
  passenger_id: string
  title: PassengerTitle
  gender: PassengerGender
  given_name: string
  family_name: string
  date_of_birth: string
  email: string
  phone_number: string
}

export interface HotelGuestDetails {
  given_name: string
  family_name: string
}

export interface ConfirmApprovalRequest {
  passengers?: PassengerDetails[] | null
  guests?: HotelGuestDetails[] | null
  contact_email?: string | null
  contact_phone_number?: string | null
  three_d_secure_session_id?: string | null
  decided_by?: string | null
}

export interface RejectApprovalRequest {
  decided_by?: string | null
  decision_notes?: string | null
}

export interface ApprovalDecisionResponse {
  booking_id: string
  approval_id: string
  booking_status: string
  provider_booking_reference: string | null
  message: string
}

export interface UserRead {
  id: string
  email: string
  full_name: string | null
  is_active: boolean
}

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface ApiErrorBody {
  detail?: string | { msg: string }[]
}

export interface OperatorProfile {
  id: string
  display_name: string
  aliases: string[]
  pronouns: string | null
  timezone_id: string
  has_real_name: boolean
  display_name_locked: boolean
  country_code: string | null
  latitude: number | null
  longitude: number | null
  location_label: string | null
}

export interface UpdateOperatorProfileRequest {
  display_name?: string | null
  aliases?: string[] | null
  pronouns?: string | null
  country_code?: string | null
  latitude?: number | null
  longitude?: number | null
  location_label?: string | null
}

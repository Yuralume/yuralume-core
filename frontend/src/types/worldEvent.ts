export interface WorldEvent {
  id: string
  source: string
  title: string
  summary: string
  url: string
  published_at: string
  fetched_at: string
  topic_tags: string[]
  has_embedding: boolean
}

export interface IngestRunResult {
  fetched: number
  new: number
  embedded: number
  evicted: number
  errors: string[]
}

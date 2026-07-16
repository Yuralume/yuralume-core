export interface ToolDescriptor {
  name: string
  description: string
  parameters_schema: Record<string, unknown>
}

export interface MessageAttachment {
  kind: string
  url: string
  mime_type: string
  caption: string | null
}

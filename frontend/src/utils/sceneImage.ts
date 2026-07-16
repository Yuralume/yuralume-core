export function resolveSceneImageUrl(imagePath: string): string {
  if (/^https?:\/\//i.test(imagePath) || imagePath.startsWith('/')) {
    return imagePath
  }
  return `/uploads/${imagePath}`
}

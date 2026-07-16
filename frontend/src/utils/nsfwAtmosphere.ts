export type NsfwAtmosphereParticleKind = 'petal' | 'star'

export interface NsfwAtmosphereParticle {
  id: string
  kind: NsfwAtmosphereParticleKind
  style: Record<string, string>
}

interface ParticleBlueprint {
  kind: NsfwAtmosphereParticleKind
  x: number
  y?: number
  size: number
  duration: number
  delay: number
  sway?: number
}

const PARTICLE_BLUEPRINTS: ParticleBlueprint[] = [
  { kind: 'petal', x: 7, size: 13, duration: 18, delay: -2, sway: 14 },
  { kind: 'petal', x: 16, size: 9, duration: 23, delay: -15, sway: -10 },
  { kind: 'petal', x: 28, size: 12, duration: 21, delay: -7, sway: 18 },
  { kind: 'petal', x: 41, size: 8, duration: 26, delay: -20, sway: -16 },
  { kind: 'petal', x: 55, size: 11, duration: 19, delay: -11, sway: 12 },
  { kind: 'petal', x: 69, size: 10, duration: 24, delay: -4, sway: -20 },
  { kind: 'petal', x: 83, size: 14, duration: 22, delay: -17, sway: 15 },
  { kind: 'petal', x: 93, size: 8, duration: 27, delay: -9, sway: -12 },
  { kind: 'star', x: 12, y: 18, size: 3, duration: 4.2, delay: -0.4 },
  { kind: 'star', x: 23, y: 62, size: 2, duration: 5.8, delay: -3.1 },
  { kind: 'star', x: 37, y: 31, size: 4, duration: 4.8, delay: -1.6 },
  { kind: 'star', x: 49, y: 78, size: 2, duration: 6.4, delay: -4.2 },
  { kind: 'star', x: 64, y: 24, size: 3, duration: 5.1, delay: -2.5 },
  { kind: 'star', x: 76, y: 69, size: 3, duration: 6, delay: -0.9 },
  { kind: 'star', x: 88, y: 39, size: 2, duration: 4.6, delay: -3.6 },
]

export function buildNsfwAtmosphereParticles(): NsfwAtmosphereParticle[] {
  return PARTICLE_BLUEPRINTS.map((particle, index) => ({
    id: `${particle.kind}-${index}`,
    kind: particle.kind,
    style: buildParticleStyle(particle),
  }))
}

function buildParticleStyle(particle: ParticleBlueprint): Record<string, string> {
  return {
    '--x': `${particle.x}vw`,
    '--y': `${particle.y ?? 0}vh`,
    '--size': `${particle.size}px`,
    '--duration': `${particle.duration}s`,
    '--delay': `${particle.delay}s`,
    '--sway': `${particle.sway ?? 0}vw`,
  }
}

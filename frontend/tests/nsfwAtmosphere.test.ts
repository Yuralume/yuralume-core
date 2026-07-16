import { describe, expect, it } from 'vitest'
import { buildNsfwAtmosphereParticles } from '../src/utils/nsfwAtmosphere'

describe('buildNsfwAtmosphereParticles', () => {
  it('returns deterministic petal and star particles for the active mode atmosphere', () => {
    const particles = buildNsfwAtmosphereParticles()

    expect(particles).toHaveLength(15)
    expect(particles.filter((particle) => particle.kind === 'petal')).toHaveLength(8)
    expect(particles.filter((particle) => particle.kind === 'star')).toHaveLength(7)
    expect(particles[0]).toEqual({
      id: 'petal-0',
      kind: 'petal',
      style: {
        '--x': '7vw',
        '--y': '0vh',
        '--size': '13px',
        '--duration': '18s',
        '--delay': '-2s',
        '--sway': '14vw',
      },
    })
  })

  it('keeps particle positioning expressed through CSS variables', () => {
    const particles = buildNsfwAtmosphereParticles()

    for (const particle of particles) {
      expect(particle.style['--x']).toMatch(/vw$/)
      expect(particle.style['--y']).toMatch(/vh$/)
      expect(particle.style['--size']).toMatch(/px$/)
      expect(particle.style['--duration']).toMatch(/s$/)
      expect(particle.style['--delay']).toMatch(/s$/)
      expect(particle.style['--sway']).toMatch(/vw$/)
    }
  })
})

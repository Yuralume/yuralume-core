<script setup lang="ts">
import { buildNsfwAtmosphereParticles } from '@/utils/nsfwAtmosphere'

const particles = buildNsfwAtmosphereParticles()
</script>

<template>
  <div class="nsfw-atmosphere" aria-hidden="true">
    <div class="nsfw-atmosphere__wash" />
    <div class="nsfw-atmosphere__frame" />
    <span
      v-for="particle in particles"
      :key="particle.id"
      class="nsfw-atmosphere__particle"
      :class="`nsfw-atmosphere__particle--${particle.kind}`"
      :style="particle.style"
    />
  </div>
</template>

<style scoped>
.nsfw-atmosphere {
  position: fixed;
  inset: 0;
  z-index: 35;
  pointer-events: none;
  overflow: hidden;
  contain: layout paint;
}

.nsfw-atmosphere__wash {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(circle at 20% 18%, rgba(255, 126, 154, 0.1), transparent 28%),
    radial-gradient(circle at 80% 72%, rgba(161, 132, 255, 0.08), transparent 30%),
    linear-gradient(120deg, rgba(255, 255, 255, 0), rgba(255, 178, 202, 0.055), rgba(255, 255, 255, 0));
  mix-blend-mode: screen;
  animation: nsfw-wash-breathe 7s ease-in-out infinite alternate;
}

.nsfw-atmosphere__frame {
  position: absolute;
  inset: 10px;
  border-radius: 18px;
  box-shadow:
    inset 0 0 24px rgba(255, 119, 155, 0.18),
    0 0 26px rgba(235, 98, 113, 0.12);
}

.nsfw-atmosphere__frame::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  padding: 1px;
  background: conic-gradient(
    from var(--flow-angle),
    rgba(255, 255, 255, 0),
    rgba(255, 151, 178, 0.72),
    rgba(178, 146, 255, 0.54),
    rgba(255, 255, 255, 0),
    rgba(255, 151, 178, 0.72),
    rgba(255, 255, 255, 0)
  );
  -webkit-mask:
    linear-gradient(#000 0 0) content-box,
    linear-gradient(#000 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
  animation: nsfw-border-flow 8s linear infinite;
}

.nsfw-atmosphere__particle {
  position: absolute;
  left: var(--x);
  width: var(--size);
  height: var(--size);
  opacity: 0;
  will-change: transform, opacity;
  animation-duration: var(--duration);
  animation-delay: var(--delay);
  animation-iteration-count: infinite;
  animation-timing-function: linear;
}

.nsfw-atmosphere__particle--petal {
  top: -10vh;
  border-radius: 80% 20% 70% 30%;
  background:
    radial-gradient(circle at 30% 25%, rgba(255, 255, 255, 0.85), transparent 26%),
    linear-gradient(145deg, rgba(255, 172, 193, 0.72), rgba(222, 91, 131, 0.36));
  box-shadow: 0 0 12px rgba(255, 138, 176, 0.2);
  animation-name: nsfw-petal-drift;
}

.nsfw-atmosphere__particle--star {
  top: var(--y);
  border-radius: 50%;
  background: rgba(255, 242, 248, 0.9);
  box-shadow:
    0 0 8px rgba(255, 226, 238, 0.8),
    0 0 16px rgba(191, 166, 255, 0.34);
  animation-name: nsfw-star-twinkle;
}

@property --flow-angle {
  syntax: '<angle>';
  inherits: false;
  initial-value: 0deg;
}

@keyframes nsfw-border-flow {
  to {
    --flow-angle: 360deg;
  }
}

@keyframes nsfw-wash-breathe {
  from {
    opacity: 0.48;
  }

  to {
    opacity: 0.82;
  }
}

@keyframes nsfw-petal-drift {
  0% {
    opacity: 0;
    transform: translate3d(0, -12vh, 0) rotate(0deg) scale(0.84);
  }

  12% {
    opacity: 0.68;
  }

  78% {
    opacity: 0.56;
  }

  100% {
    opacity: 0;
    transform: translate3d(var(--sway), 118vh, 0) rotate(420deg) scale(1.05);
  }
}

@keyframes nsfw-star-twinkle {
  0%,
  100% {
    opacity: 0.12;
    transform: scale(0.72);
  }

  45% {
    opacity: 0.76;
    transform: scale(1.55);
  }
}

@media (prefers-reduced-motion: reduce) {
  .nsfw-atmosphere__wash,
  .nsfw-atmosphere__frame::before,
  .nsfw-atmosphere__particle {
    animation: none;
  }

  .nsfw-atmosphere__particle {
    display: none;
  }
}
</style>

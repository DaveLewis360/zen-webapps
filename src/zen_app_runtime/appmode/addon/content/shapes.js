/* zen-app-runtime app mode — Material 3 Expressive shapes.
 * Every shape is a radius function r(θ) on the unit circle so any two shapes
 * can morph into each other by interpolating the same samples. */
"use strict";

const ZenShapes = (() => {
  const SAMPLES = 180;

  function superellipse(p, a = 1, b = 1) {
    return theta => {
      const c = Math.abs(Math.cos(theta)) / a;
      const s = Math.abs(Math.sin(theta)) / b;
      return (c ** p + s ** p) ** (-1 / p);
    };
  }

  const wave = (lobes, amount, sharpness = 1) => theta => {
    const value = Math.cos(lobes * theta);
    const shaped = Math.sign(value) * Math.abs(value) ** sharpness;
    return 1 + amount * shaped;
  };

  const KINDS = {
    circle: () => 1,
    cookie4: wave(4, 0.1),
    cookie6: wave(6, 0.085),
    cookie7: wave(7, 0.08),
    cookie9: wave(9, 0.075),
    cookie12: wave(12, 0.055),
    clover4: wave(4, 0.24, 0.75),
    clover8: wave(8, 0.15, 0.8),
    flower: wave(8, 0.12),
    sunny: wave(8, 0.045),
    burst: wave(12, 0.14, 0.55),
    softBurst: wave(10, 0.1, 0.8),
    puffy: wave(6, 0.13, 0.5),
    squircle: superellipse(4.2),
    pill: superellipse(2.6, 1, 0.72),
    gem: wave(5, 0.1, 1.2),
  };

  function radii(kind) {
    const fn = KINDS[kind] || KINDS.circle;
    const out = new Float64Array(SAMPLES);
    let max = 0;
    for (let i = 0; i < SAMPLES; i++) {
      const theta = (2 * Math.PI * i) / SAMPLES;
      out[i] = fn(theta);
      max = Math.max(max, out[i]);
    }
    for (let i = 0; i < SAMPLES; i++) {
      out[i] /= max;
    }
    return out;
  }

  function toPath(rs, cx = 50, cy = 50, size = 48) {
    let d = "";
    for (let i = 0; i < SAMPLES; i++) {
      const theta = (2 * Math.PI * i) / SAMPLES - Math.PI / 2;
      const x = cx + rs[i] * size * Math.cos(theta);
      const y = cy + rs[i] * size * Math.sin(theta);
      d += `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`;
    }
    return d + "Z";
  }

  // Damped spring (M3 "fast spatial": damping 0.6, stiffness 800).
  function spring(progress, damping = 0.62, stiffness = 520) {
    const omega = Math.sqrt(stiffness);
    const t = progress * 0.9;
    const wd = omega * Math.sqrt(1 - damping * damping);
    return 1 - Math.exp(-damping * omega * t) * (Math.cos(wd * t) + ((damping * omega) / wd) * Math.sin(wd * t));
  }

  class Morph {
    constructor(path, kind, { size = 48 } = {}) {
      this.path = path;
      this.size = size;
      this.current = radii(kind);
      this.frame = 0;
      path.setAttribute("d", toPath(this.current, 50, 50, size));
    }

    to(kind, duration = 900) {
      const from = Float64Array.from(this.current);
      const target = radii(kind);
      const start = performance.now();
      const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
      cancelAnimationFrame(this.frame);
      const step = now => {
        const p = reduce ? 1 : Math.min(1, (now - start) / duration);
        const k = reduce ? 1 : spring(p);
        for (let i = 0; i < SAMPLES; i++) {
          this.current[i] = from[i] + (target[i] - from[i]) * k;
        }
        this.path.setAttribute("d", toPath(this.current, 50, 50, this.size));
        if (p < 1) {
          this.frame = requestAnimationFrame(step);
        }
      };
      this.frame = requestAnimationFrame(step);
    }
  }

  return { radii, toPath, Morph, kinds: Object.keys(KINDS) };
})();

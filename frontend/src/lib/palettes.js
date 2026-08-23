// Palettes du waterfall : une table de 256 couleurs précalculée, appliquée
// ensuite octet par octet. Précalculer évite de refaire l’interpolation pour
// chaque pixel de chaque ligne, 25 fois par seconde.
function ramp(stops) {
  const lut = new Uint8Array(256 * 3)
  for (let i = 0; i < 256; i++) {
    const t = i / 255
    let a = stops[0], b = stops[stops.length - 1]
    for (let k = 0; k < stops.length - 1; k++) {
      if (t >= stops[k][0] && t <= stops[k + 1][0]) { a = stops[k]; b = stops[k + 1]; break }
    }
    const span = (b[0] - a[0]) || 1
    const u = (t - a[0]) / span
    lut[i * 3 + 0] = a[1] + (b[1] - a[1]) * u
    lut[i * 3 + 1] = a[2] + (b[2] - a[2]) * u
    lut[i * 3 + 2] = a[3] + (b[3] - a[3]) * u
  }
  return lut
}

export const PALETTES = {
  turbo: ramp([
    [0.0, 12, 12, 40], [0.25, 30, 120, 220], [0.5, 40, 200, 140],
    [0.7, 230, 220, 60], [0.85, 240, 120, 40], [1.0, 160, 20, 20],
  ]),
  viridis: ramp([
    [0.0, 68, 1, 84], [0.35, 59, 82, 139], [0.6, 33, 145, 140],
    [0.85, 94, 201, 98], [1.0, 253, 231, 37],
  ]),
  gris: ramp([[0, 0, 0, 0], [1, 255, 255, 255]]),
  braise: ramp([
    [0.0, 6, 6, 12], [0.4, 120, 20, 10], [0.7, 240, 120, 20], [1.0, 255, 250, 210],
  ]),
}

export const paletteOf = (name) => PALETTES[name] || PALETTES.turbo

// Living background: faint tile grid, a spotlight that follows the pointer,
// and tiles that occasionally light up with one of the logo glyphs.

const CELL = 72;
const GLYPHS = ['g-ring', 'g-diamond', 'g-rect', 'g-plus'];

export function startBackground() {
  const root = document.documentElement;
  let raf = 0;
  addEventListener('pointermove', (e) => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      root.style.setProperty('--mx', `${e.clientX}px`);
      root.style.setProperty('--my', `${e.clientY}px`);
      raf = 0;
    });
  }, { passive: true });

  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const box = document.getElementById('bgTiles');
  if (!box) return;

  const spawn = () => {
    if (document.hidden || box.childElementCount > 7) return;
    const w = innerWidth, h = innerHeight;
    const cols = Math.ceil(w / CELL), rows = Math.ceil(h / CELL);
    for (let tries = 0; tries < 8; tries++) {
      const c = Math.floor(Math.random() * cols), r = Math.floor(Math.random() * rows);
      if (r * CELL < 110) continue; // keep the header (logo, login button) clean
      // stay inside the visible (masked) ellipse of the grid
      const nx = ((c + 0.5) * CELL) / w - 0.5, ny = ((r + 0.5) * CELL) / h - 0.4;
      if ((nx / 0.6) ** 2 + (ny / 0.5) ** 2 > 1) continue;
      const el = document.createElement('div');
      el.className = 'ftile' + (Math.random() < 0.2 ? ' blue' : '');
      el.style.left = `${c * CELL + 6}px`;
      el.style.top = `${r * CELL + 6}px`;
      el.innerHTML = `<svg><use href="#${GLYPHS[Math.floor(Math.random() * GLYPHS.length)]}"/></svg>`;
      el.addEventListener('animationend', () => el.remove());
      box.appendChild(el);
      return;
    }
  };
  setInterval(spawn, 700);
}

// Brand intro, recreated from the SupplierHub logo video:
// dot -> spinning square -> splits into 4 tiles with a ring pulse -> 3 tiles turn white ->
// glyphs roll in like a slot machine -> wordmark wipes in -> the whole lockup flies into the header.

const EASE = 'cubic-bezier(.2,.8,.2,1)';
const SPRING = 'cubic-bezier(.34,1.56,.64,1)';

export function playIntro({ full }) {
  const intro = document.getElementById('intro');
  const body = document.body;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const done = () => { body.classList.remove('intro-on'); intro?.remove(); };
  if (!intro || reduce || !full || !intro.animate) {
    done();
    return Promise.resolve();
  }

  const lockup = document.getElementById('introLockup');
  const mark = lockup.querySelector('.mark');
  const tiles = [...mark.querySelectorAll('.tile')];
  const glyphs = tiles.map((t) => t.querySelector('svg'));
  const seed = mark.querySelector('.seed');
  const ring = mark.querySelector('.ring-pulse');
  const word = lockup.querySelector('.word');
  const tag = word.querySelector('small');
  const brandMark = document.querySelector('#brand .mark');

  const anims = [];
  const A = (el, frames, opts) => {
    const a = el.animate(frames, { fill: 'both', easing: EASE, ...opts });
    a.playbackRate = 1.7;
    anims.push(a);
    return a;
  };

  // --- geometry (measured before any transform is applied)
  const gap = parseFloat(getComputedStyle(lockup).columnGap || getComputedStyle(lockup).gap) || 0;
  const shift = (word.offsetWidth + gap) / 2; // keeps the mark centred until the word appears
  const lockRect = lockup.getBoundingClientRect();
  const markRect = mark.getBoundingClientRect();
  const target = brandMark.getBoundingClientRect();
  const k = target.width / markRect.width;
  const dx = target.left - lockRect.left - k * (markRect.left - lockRect.left);
  const dy = target.top - lockRect.top - k * (markRect.top - lockRect.top);
  const tagVisibleInHeader = getComputedStyle(document.querySelector('#brand .word small')).display !== 'none';

  const size = tiles[0].offsetWidth;
  const tgap = parseFloat(getComputedStyle(mark).gap) || 0;
  const half = (size + tgap) / 2;

  // 1. seed: dot -> rounded square, with a twist
  A(seed, [
    { transform: 'scale(0) rotate(-60deg)', borderRadius: '50%' },
    { transform: 'scale(1.35) rotate(12deg)', borderRadius: '34%', offset: 0.7 },
    { transform: 'scale(1.2) rotate(0deg)', borderRadius: '26%' },
  ], { duration: 560, easing: SPRING });
  A(seed, [{ opacity: 1 }, { opacity: 0 }], { duration: 120, delay: 560 });

  // 2. split into four tiles + ring pulse
  tiles.forEach((t, i) => {
    const sx = i % 2 === 0 ? half : -half;
    const sy = i < 2 ? half : -half;
    A(t, [
      { transform: `translate(${sx}px, ${sy}px) scale(.5)`, opacity: 0 },
      { transform: `translate(${sx}px, ${sy}px) scale(.5)`, opacity: 1, offset: 0.01 },
      { transform: 'translate(0,0) scale(1)', opacity: 1 },
    ], { duration: 520, delay: 540 + i * 35, easing: SPRING });
  });
  A(ring, [
    { transform: 'scale(.55)', opacity: 0.9 },
    { transform: 'scale(1.75)', opacity: 0 },
  ], { duration: 800, delay: 560 });

  // 3. three tiles turn white, the fourth stays blue
  tiles.slice(0, 3).forEach((t, i) => {
    A(t, [{ backgroundColor: '#2B59FF' }, { backgroundColor: '#F7F7F5' }], { duration: 300, delay: 980 + i * 70 });
  });

  // 4. glyphs roll in (slot machine)
  glyphs.forEach((g, i) => {
    A(g, [
      { transform: 'translateY(-115%)', filter: 'blur(5px)' },
      { transform: 'translateY(12%)', filter: 'blur(1px)', offset: 0.7 },
      { transform: 'translateY(0)', filter: 'blur(0)' },
    ], { duration: 520, delay: 1180 + i * 95, easing: 'cubic-bezier(.3,.7,.3,1)' });
  });

  // 5. blue tile glow
  A(tiles[3], [
    { boxShadow: '0 0 0 0 rgba(43,89,255,0)' },
    { boxShadow: '0 0 38px 6px rgba(43,89,255,.55)', offset: 0.4 },
    { boxShadow: '0 0 0 0 rgba(43,89,255,0)' },
  ], { duration: 900, delay: 1620 });

  // 6. mark slides left, wordmark wipes in, tagline settles
  A(lockup, [{ transform: `translateX(${shift}px)` }, { transform: `translateX(${shift}px)`, offset: 0.001 },
    { transform: 'translateX(0)' }], { duration: 620, delay: 1850 });
  A(word, [{ clipPath: 'inset(0 100% 0 0)' }, { clipPath: 'inset(0 0% 0 0)' }], { duration: 620, delay: 1900 });
  A(tag, [{ opacity: 0, letterSpacing: '.7em' }, { opacity: 1, letterSpacing: '.3em' }], { duration: 600, delay: 2250 });

  // 7. fly into the header ('forwards' only: must not override the earlier transform while waiting)
  const FLY = 3000;
  A(lockup, [{ transform: 'translate(0,0) scale(1)' }, { transform: `translate(${dx}px, ${dy}px) scale(${k})` }],
    { duration: 620, delay: FLY, easing: 'cubic-bezier(.65,0,.25,1)', fill: 'forwards' });
  if (!tagVisibleInHeader) A(tag, [{ opacity: 1 }, { opacity: 0 }], { duration: 250, delay: FLY, fill: 'forwards' });
  A(intro, [{ backgroundColor: 'rgba(11,11,12,1)' }, { backgroundColor: 'rgba(11,11,12,0)' }],
    { duration: 520, delay: FLY + 100 });

  const skip = () => anims.forEach((a) => a.finish());
  intro.addEventListener('pointerdown', skip, { once: true });
  addEventListener('keydown', skip, { once: true });

  return Promise.all(anims.map((a) => a.finished.catch(() => {}))).then(() => {
    removeEventListener('keydown', skip);
    body.classList.remove('intro-on');
    const out = intro.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 160, fill: 'forwards' });
    out.finished.then(() => intro.remove());
  });
}

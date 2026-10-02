import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('./layout.js', import.meta.url), 'utf8');
const { justifiedLayout, visibleRects } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
let seed = 72461;
const random = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
let cases = 0;
for (const width of [320, 768, 1280, 1920, 3440]) {
  for (const count of [0, 1, 2, 9, 121, 1000]) {
    const items = Array.from({ length: count }, (_, id) => ({ id, width: 80 + random() * 1920, height: 80 + random() * 1920 }));
    const result = justifiedLayout(items, width, 320, 6);
    assert.equal(result.rects.length, items.length);
    for (const rect of result.rects) {
      assert.ok(rect.x >= -1e-8 && rect.y >= 0 && rect.width > 0 && rect.height > 0);
      assert.ok(rect.x + rect.width <= width + 1e-7);
      assert.ok(Math.abs(rect.width / rect.height - items[rect.index].width / items[rect.index].height) < 1e-8);
      assert.ok(rect.y + rect.height <= result.height + 1e-7);
    }
    const originalHeight = result.height;
    for (let top = 0; top <= result.height; top += 193) {
      const visible = visibleRects(result.rects, top, 700);
      assert.equal(new Set(visible.map(x => x.id)).size, visible.length);
      assert.equal(result.height, originalHeight);
    }
    assert.deepEqual(justifiedLayout(items, width, 320, 6), result);
    cases++;
  }
}
console.log(`PASS: ${cases} aspect/bounds/identity/scroll-layout property cases`);

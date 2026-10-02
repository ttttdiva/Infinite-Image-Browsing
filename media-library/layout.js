// 画像の実寸比から配置を確定する。画像ロードやDOMの高さでscrollHeightを変えない。
export function justifiedLayout(items, availableWidth, targetHeight = 320, gap = 6) {
  const width = Math.max(1, availableWidth);
  const target = Math.max(80, targetHeight);
  const rects = [];
  let start = 0, y = 0;
  while (start < items.length) {
    let end = start, ratioSum = 0;
    while (end < items.length) {
      const item = items[end];
      const ratio = item.width > 0 && item.height > 0 ? item.width / item.height : 1;
      ratioSum += ratio;
      end++;
      if (ratioSum * target + (end - start - 1) * gap >= width) break;
    }
    if (end - start > 1 && end < items.length) {
      const last = items[end - 1];
      const lastRatio = last.width > 0 && last.height > 0 ? last.width / last.height : 1;
      const withLast = (width - (end - start - 1) * gap) / ratioSum;
      const withoutLast = (width - (end - start - 2) * gap) / (ratioSum - lastRatio);
      if (Math.abs(withoutLast - target) < Math.abs(withLast - target) && withoutLast < target * 1.6) {
        end--; ratioSum -= lastRatio;
      }
    }
    const fitHeight = Math.max(1, (width - (end - start - 1) * gap) / ratioSum);
    const height = end === items.length ? Math.min(target, fitHeight) : fitHeight;
    let x = 0;
    for (let i = start; i < end; i++) {
      const item = items[i];
      const ratio = item.width > 0 && item.height > 0 ? item.width / item.height : 1;
      const w = ratio * height;
      rects.push({ id: item.id, index: i, x, y, width: w, height });
      x += w + gap;
    }
    y += height + gap;
    start = end;
  }
  return { rects, height: Math.max(0, y - gap) };
}

export function visibleRects(rects, scrollTop, viewportHeight, overscan = 650) {
  const top = Math.max(0, scrollTop - overscan);
  const bottom = scrollTop + viewportHeight + overscan;
  let low = 0, high = rects.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (rects[mid].y + rects[mid].height < top) low = mid + 1;
    else high = mid;
  }
  const result = [];
  for (let i = low; i < rects.length && rects[i].y <= bottom; i++) result.push(rects[i]);
  return result;
}

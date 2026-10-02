export function ratingFromKey(key, maxRating) {
  if (maxRating === 10 && key === '0') return 10;
  if (!/^[1-9]$/.test(key)) return null;
  const value = Number(key);
  return value <= maxRating ? value : null;
}

export function shortcutTargetIds({ viewerId = null, selected = [], activeId = null, lastClicked = null }) {
  if (viewerId !== null && viewerId !== undefined) return [viewerId];
  if (selected.length) return [...new Set(selected)];
  const fallback = activeId ?? lastClicked;
  return fallback === null || fallback === undefined ? [] : [fallback];
}

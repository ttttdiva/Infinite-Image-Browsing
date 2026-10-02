import assert from 'node:assert/strict';
import { ratingFromKey, shortcutTargetIds } from './rating-shortcuts.js';

assert.equal(ratingFromKey('1', 5), 1);
assert.equal(ratingFromKey('5', 5), 5);
assert.equal(ratingFromKey('6', 5), null);
assert.equal(ratingFromKey('0', 5), null);
assert.equal(ratingFromKey('9', 10), 9);
assert.equal(ratingFromKey('0', 10), 10);
assert.equal(ratingFromKey('x', 10), null);
assert.deepEqual(shortcutTargetIds({viewerId: 9, selected:[1,2], activeId:3}), [9]);
assert.deepEqual(shortcutTargetIds({selected:[1,2,1], activeId:3}), [1,2]);
assert.deepEqual(shortcutTargetIds({activeId:3}), [3]);
assert.deepEqual(shortcutTargetIds({lastClicked:4}), [4]);
console.log('rating shortcut tests passed');

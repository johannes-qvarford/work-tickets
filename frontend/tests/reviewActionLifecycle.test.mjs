import assert from "node:assert/strict";
import test from "node:test";
import {
  beginReviewAction,
  completeReviewAction,
  failReviewAction,
  reviewActionDisabled,
} from "../src/reviewActionLifecycle.ts";

test("a failed review action is reset to an enabled retry", () => {
  const states = {};
  const errors = {};
  const successes = {};

  beginReviewAction(states, errors, successes, "WORK-1");
  assert.equal(reviewActionDisabled(false, true, states["WORK-1"]), true);

  failReviewAction(states, errors, "WORK-1", "Merge timed out.");
  assert.equal(states["WORK-1"], undefined);
  assert.equal(errors["WORK-1"], "Merge timed out.");
  assert.equal(reviewActionDisabled(false, true, states["WORK-1"]), false);
});

test("retry clears the old failure and successful completion disables while loading only", () => {
  const states = {};
  const errors = {};
  const successes = {};

  failReviewAction(states, errors, "WORK-2", "Temporary failure.");
  beginReviewAction(states, errors, successes, "WORK-2");
  assert.equal(errors["WORK-2"], undefined);
  assert.equal(reviewActionDisabled(false, true, states["WORK-2"]), true);

  completeReviewAction(states, successes, "WORK-2", "Review completed.");
  assert.equal(states["WORK-2"], "success");
  assert.equal(successes["WORK-2"], "Review completed.");
  assert.equal(reviewActionDisabled(false, true, states["WORK-2"]), false);
  assert.equal(reviewActionDisabled(true, true, states["WORK-2"]), true);
});

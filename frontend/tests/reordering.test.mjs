import assert from "node:assert/strict";
import test from "node:test";
import {
  displayedDropTargetIndex,
  handleSubtaskDragEnter,
  handleSubtaskDragOver,
  handleTicketDragEnter,
  handleTicketDragOver,
  isAfterDropTarget,
} from "../src/reordering.ts";

function dragEvent(clientY, type) {
  const event = {
    clientY,
    currentTarget: { getBoundingClientRect: () => ({ top: 100, height: 40 }) },
    dataTransfer: { types: [type], dropEffect: "none" },
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  return event;
}

const tickets = [
  { id: 1, local_completed: false, category_id: 10 },
  { id: 2, local_completed: false, category_id: 20 },
  { id: 3, local_completed: false, category_id: 10 },
  { id: 4, local_completed: true, category_id: 10 },
];

test("pointer position selects the before or after half of a drop target", () => {
  const target = { top: 100, height: 40 };

  assert.equal(isAfterDropTarget(120, target), false);
  assert.equal(isAfterDropTarget(121, target), true);
});

test("top-level dragenter feedback is applied immediately", () => {
  const event = dragEvent(120, "application/x-work-tickets-ticket");
  const emitted = [];

  handleTicketDragEnter(event, {
    ticketId: 2,
    ticketCanDrag: true,
    draggingTicketId: 1,
    clearSubtaskDragOverState() { throw new Error("subtask state should not be cleared"); },
    onFeedback(afterTarget) { emitted.push([2, afterTarget]); },
  });

  assert.deepEqual(emitted, [[2, false]]);
  assert.equal(event.prevented, true);
  assert.equal(event.stopped, true);
  assert.equal(event.dataTransfer.dropEffect, "move");
});

test("top-level dragover rejects self and non-draggable targets without feedback", () => {
  const feedback = [];
  const clearCalls = [];

  for (const options of [
    { ticketCanDrag: false, draggingTicketId: 1 },
    { ticketCanDrag: true, draggingTicketId: 2 },
  ]) {
    const event = dragEvent(121, "application/x-work-tickets-ticket");
    handleTicketDragOver(event, {
      ticketId: 2,
      ...options,
      clearSubtaskDragOverState() { clearCalls.push(true); },
      onFeedback(afterTarget) { feedback.push(afterTarget); },
    });
    assert.equal(event.prevented, false);
  }

  assert.deepEqual(feedback, []);
  assert.deepEqual(clearCalls, []);
});

test("top-level dragover clears subtask feedback for a subtask drag", () => {
  const event = dragEvent(121, "application/x-work-tickets-subtask");
  let clearCalls = 0;

  handleTicketDragOver(event, {
    ticketId: 2,
    ticketCanDrag: true,
    draggingTicketId: 1,
    clearSubtaskDragOverState() { clearCalls += 1; },
    onFeedback() { throw new Error("subtask drags must not emit ticket feedback"); },
  });

  assert.equal(clearCalls, 1);
  assert.equal(event.prevented, false);
});

test("top-level dragenter feedback switches from before to after at the target midpoint", () => {
  const event = dragEvent(120, "application/x-work-tickets-ticket");
  const emitted = [];

  const options = {
    ticketId: 2,
    ticketCanDrag: true,
    draggingTicketId: 1,
    clearSubtaskDragOverState() {},
    onFeedback(afterTarget) { emitted.push(afterTarget); },
  };

  handleTicketDragEnter(event, options);
  event.clientY = 121;
  handleTicketDragOver(event, options);

  assert.deepEqual(emitted, [false, true]);
});

test("subtask dragenter updates the target and feedback immediately", () => {
  const event = dragEvent(121, "application/x-work-tickets-subtask");
  const state = [];

  handleSubtaskDragEnter(event, {
    parentId: 10,
    subtaskId: 12,
    subtaskCompleted: false,
    draggingParentId: 10,
    draggingSubtaskId: 11,
    clearSubtaskDragOverState() { throw new Error("valid subtask target should not clear state"); },
    onTarget() { state.push([10, 12]); },
    onFeedback(afterTarget) { state.push(afterTarget); },
  });

  assert.deepEqual(state, [[10, 12], true]);
  assert.equal(event.prevented, true);
  assert.equal(event.stopped, true);
  assert.equal(event.dataTransfer.dropEffect, "move");
});

test("subtask dragover rejects completed, foreign-parent, and self targets", () => {
  const state = [];
  const clearCalls = [];

  for (const options of [
    { subtaskCompleted: true, draggingParentId: 10, draggingSubtaskId: 11 },
    { subtaskCompleted: false, draggingParentId: 20, draggingSubtaskId: 11 },
    { subtaskCompleted: false, draggingParentId: 10, draggingSubtaskId: 12 },
  ]) {
    const event = dragEvent(121, "application/x-work-tickets-subtask");
    handleSubtaskDragOver(event, {
      parentId: 10,
      subtaskId: 12,
      ...options,
      clearSubtaskDragOverState() { clearCalls.push(true); },
      onTarget() { state.push("target"); },
      onFeedback() { state.push("feedback"); },
    });
    assert.equal(event.prevented, false);
  }

  assert.deepEqual(state, []);
  assert.equal(clearCalls.length, 3);
});

test("subtask dragenter feedback switches at the target midpoint", () => {
  const event = dragEvent(120, "application/x-work-tickets-subtask");
  const feedback = [];
  const options = {
    parentId: 10,
    subtaskId: 12,
    subtaskCompleted: false,
    draggingParentId: 10,
    draggingSubtaskId: 11,
    clearSubtaskDragOverState() {},
    onTarget() {},
    onFeedback(afterTarget) { feedback.push(afterTarget); },
  };

  handleSubtaskDragEnter(event, options);
  event.clientY = 121;
  handleSubtaskDragOver(event, options);

  assert.deepEqual(feedback, [false, true]);
});

test("filtered queue keeps the displayed target's before and after insertion indexes", () => {
  const displayed = (ticket) => ticket.category_id === 10;

  // Ticket 2 is active but hidden between the two displayed targets.
  assert.equal(displayedDropTargetIndex(tickets, 1, 3, false, displayed), 1);
  assert.equal(displayedDropTargetIndex(tickets, 1, 3, true, displayed), 2);
  assert.equal(displayedDropTargetIndex(tickets, 2, 3, false, displayed), null);
});

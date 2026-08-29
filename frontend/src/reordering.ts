import { ref } from "vue";

export interface ReorderableItem {
  id: number;
  local_completed: boolean;
  category_id?: number | null;
}

export const draggingSubtaskId = ref<number | null>(null);
export const draggingSubtaskParentId = ref<number | null>(null);
export const dragOverSubtaskId = ref<number | null>(null);
export const dragOverSubtaskParentId = ref<number | null>(null);

export function clearSubtaskDragOverState() {
  dragOverSubtaskParentId.value = null;
  dragOverSubtaskId.value = null;
}

export function clearSubtaskDragState() {
  draggingSubtaskParentId.value = null;
  draggingSubtaskId.value = null;
  clearSubtaskDragOverState();
}

export function displayedActiveItems(
  items: readonly ReorderableItem[],
  isDisplayed: (item: ReorderableItem) => boolean,
): ReorderableItem[] {
  return items.filter((item) => !item.local_completed && isDisplayed(item));
}

/** Return the server target index after removing the dragged active item. */
export function dropTargetIndex(
  items: readonly ReorderableItem[],
  sourceId: number,
  targetId: number,
  afterTarget: boolean,
): number | null {
  const activeItems = items.filter((item) => !item.local_completed);
  const sourceIndex = activeItems.findIndex((item) => item.id === sourceId);
  const targetIndex = activeItems.findIndex((item) => item.id === targetId);
  if (sourceIndex < 0 || targetIndex < 0 || sourceId === targetId) return null;

  const adjustedTargetIndex = targetIndex > sourceIndex ? targetIndex - 1 : targetIndex;
  return adjustedTargetIndex + (afterTarget ? 1 : 0);
}

/** Translate a displayed reorder into the global active insertion index. */
export function displayedDropTargetIndex(
  items: readonly ReorderableItem[],
  sourceId: number,
  targetId: number,
  afterTarget: boolean,
  isDisplayed: (item: ReorderableItem) => boolean,
): number | null {
  const displayedItems = displayedActiveItems(items, isDisplayed);
  if (!displayedItems.some((item) => item.id === sourceId)) return null;
  if (!displayedItems.some((item) => item.id === targetId)) return null;
  return dropTargetIndex(items, sourceId, targetId, afterTarget);
}

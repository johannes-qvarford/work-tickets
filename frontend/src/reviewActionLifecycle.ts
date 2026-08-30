export type ReviewActionStatus = "loading" | "success";

export type ReviewActionStates = Record<string, ReviewActionStatus | undefined>;
export type ReviewActionMessages = Record<string, string | undefined>;

export function beginReviewAction(
  states: ReviewActionStates,
  errors: ReviewActionMessages,
  successes: ReviewActionMessages,
  key: string,
): void {
  states[key] = "loading";
  delete errors[key];
  delete successes[key];
}

export function completeReviewAction(
  states: ReviewActionStates,
  successes: ReviewActionMessages,
  key: string,
  message: string,
): void {
  states[key] = "success";
  successes[key] = message;
}

export function failReviewAction(
  states: ReviewActionStates,
  errors: ReviewActionMessages,
  key: string,
  message: string,
): void {
  delete states[key];
  errors[key] = message;
}

export function reviewActionDisabled(
  hasReviewError: boolean,
  enabled: boolean,
  status: ReviewActionStatus | undefined,
): boolean {
  return hasReviewError || !enabled || status === "loading";
}

/** The card pager loops: after the last card comes the first, before the first the last. */
export const cardNext = (index: number, total: number) => (index + 1) % total;
export const cardPrev = (index: number, total: number) =>
  (index - 1 + total) % total;

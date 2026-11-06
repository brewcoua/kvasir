// One clock for the page, so every running timer moves on the same tick.
export const clock = $state({ now: Date.now() / 1000 });

setInterval(() => (clock.now = Date.now() / 1000), 1000);

export function elapsed(from: number | null, to: number | null): number | null {
  return from ? (to ?? clock.now) - from : null;
}

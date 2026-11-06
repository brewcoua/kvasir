// The one question on screen, asked from anywhere and answered by the dialog in `Confirm.svelte`.
interface Question {
  text: string;
  action: string;
  answer: (yes: boolean) => void;
}

export const asking = $state<{ question: Question | null }>({ question: null });

export function sure(text: string, action = "Delete"): Promise<boolean> {
  // A question still open cannot be answered any more once this one replaces it.
  asking.question?.answer(false);
  return new Promise((answer) => {
    asking.question = { text, action, answer };
  });
}

export function reply(yes: boolean) {
  asking.question?.answer(yes);
  asking.question = null;
}

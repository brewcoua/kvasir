// Starting a run, shared by every layout of the new run page.
import { METHODS, type Method, type Picked } from "./methods";
import { chosen } from "./settings.svelte";

// The method last picked, so the page opens on it.
const KEY = "method";

export class Launch {
  method: Method = $state(METHODS.find((m) => m.kind === localStorage.getItem(KEY)) ?? METHODS[0]);
  picked: Picked = $state({});
  text = $state("");
  // The questions `/v1/clarify` asked, while the user answers them. Null otherwise.
  asked: string[] | null = $state(null);
  answers: string[] = $state([]);
  status = $state("");
  busy = $state(false);

  constructor(private onstarted: (id: string) => void) {}

  reset = () => {
    this.asked = null;
    this.answers = [];
  };

  choose = (kind: string) => {
    this.method = METHODS.find((m) => m.kind === kind)!;
    this.picked = {};
    this.reset();
    localStorage.setItem(KEY, kind);
  };

  submit = async () => {
    if (!this.method.clarify) return this.start();
    if (this.asked !== null) return this.start(true);

    this.status = "checking whether the question needs clarifying…";
    this.busy = true;
    try {
      const response = await fetch("/v1/clarify", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: this.text.trim() }),
      });
      if (!response.ok) throw new Error(`clarify: ${response.status}`);
      const { questions } = await response.json();
      if (questions.length > 0) {
        this.asked = questions;
        this.answers = questions.map(() => "");
        this.status = "";
        return;
      }
    } catch (error) {
      // Clarifying is a courtesy. Failing to, the run starts as asked.
      console.warn(error);
    } finally {
      this.busy = false;
    }
    return this.start();
  };

  // Posts the run and reads only far enough to learn its id, then follows it like any other run.
  // The rest of the stream is still read, and thrown away: the server gives the run slot back when
  // this response ends, so ending it early would free the slot mid-run.
  start = async (answered = false) => {
    const clarifications = answered
      ? (this.asked ?? [])
          .map((question, i) => ({ question, answer: (this.answers[i] ?? "").trim() }))
          .filter((clarification) => clarification.answer !== "")
      : [];
    const body = {
      method: this.method.name,
      input: { question: this.text.trim(), ...(this.method.clarify ? { clarifications } : {}) },
      options: { ...this.picked, ...chosen(this.method.kind) },
    };
    this.busy = true;
    this.status = "starting…";
    try {
      const response = await fetch("/v1/runs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok || !response.body) {
        const error = await response.json().catch(() => ({}));
        this.status = `refused: ${error.message ?? JSON.stringify(error.detail ?? response.status)}`;
        return;
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      let runId: string | null = null;
      while (runId === null) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        const frame = buffer.match(/event: run\ndata: (.*)\n\n/);
        if (frame) runId = JSON.parse(frame[1]).run_id;
      }
      if (runId === null) {
        this.status = "the run ended before it started";
        return;
      }
      (async () => {
        while (!(await reader.read()).done);
      })().catch(() => {});
      this.reset();
      this.picked = {};
      this.text = "";
      this.status = "";
      this.onstarted(runId);
    } catch (error) {
      this.status = String(error);
    } finally {
      this.busy = false;
    }
  };
}

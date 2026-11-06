// Icons of the site graph. The graph asks for hundreds at once, and each is a request that can wait 10s on a site, so
// they load a few at a time, biggest first, and a node draws its icon only once the browser holds it.
import { SvelteSet } from "svelte/reactivity";

// Under the browser's 6 connections per host, which the API shares, and under the service's 8 lookup workers: a request
// made here starts at once, so it does not time out in a queue on the service.
const AT_ONCE = 4;
// Past the 60s the browser keeps a 404 that means "ask again", since a lookup can finish after its answer went out.
const RETRY_MS = 65_000;

export const iconUrl = (host: string) => `/v1/favicons/${encodeURIComponent(host)}`;

export const loaded = new SvelteSet<string>();
// Hosts being loaded, waiting to retry, or given up on: none of these go in the queue again.
const seen = new Set<string>();
const retried = new Set<string>();
let queue: string[] = [];
let running = 0;

function next() {
  while (running < AT_ONCE && queue.length > 0) load(queue.shift()!);
}

function load(host: string) {
  running++;
  seen.add(host);
  const image = new Image();
  const finish = () => {
    running--;
    next();
  };
  image.onload = () => {
    loaded.add(host);
    finish();
  };
  image.onerror = () => {
    if (!retried.has(host)) {
      retried.add(host);
      setTimeout(() => {
        queue.unshift(host);
        next();
      }, RETRY_MS);
    }
    finish();
  };
  image.src = iconUrl(host);
}

// In the order to load them. Replaces what was still waiting, so an empty list stops the rest.
export function wantIcons(hosts: string[]) {
  queue = hosts.filter((host) => !seen.has(host));
  next();
}

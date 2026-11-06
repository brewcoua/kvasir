export const count = (value: number) => Number(value).toLocaleString();

export function size(value: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let at = 0;
  while (value >= 1024 && at < units.length - 1) {
    value /= 1024;
    at++;
  }
  return `${at === 0 ? value : value.toFixed(1)} ${units[at]}`;
}

export function duration(total: number): string {
  if (total < 60) return `${total.toFixed(1)}s`;
  const whole = Math.floor(total);
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m ${whole % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// Only a web link becomes clickable: the text comes from web pages and the model.
export const isWeb = (url: string) => /^https?:/i.test(url);

// Output tokens over the whole call, prompt processing included, since nothing is streamed.
export const rate = (tokens: number | null, seconds: number) =>
  tokens && seconds > 0 ? (tokens / seconds).toFixed(1) : "";

// The short label of a source kind, for a badge. A mark may be any of these but `unknown`.
export const KIND_LABELS: Record<string, string> = {
  intergovernmental: "intergov",
  government: "gov",
  academic: "academic",
  standards: "standards",
  news: "news",
  reference: "reference",
  organization: "org",
  unknown: "unknown",
};

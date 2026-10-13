import type { components } from "./schema";

export type Status = components["schemas"]["Status"];
export type Settings = components["schemas"]["Settings"];

export class ApiError extends Error {}

/** POST to the jukebox API. The X-Jukebox header is the server's CSRF guard. */
export async function post<T = { ok: boolean }>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Jukebox": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(data.error ?? `Ошибка запроса (${response.status})`);
  return data as T;
}

export const api = {
  play: (track: string) => post("play", { track }),
  stop: () => post("stop"),
  seek: (position: number) => post("seek", { position }),
};
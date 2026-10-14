import type { components } from "./schema";

export type Status = components["schemas"]["Status"];
export type Settings = components["schemas"]["Settings"];
export type Track = components["schemas"]["Track"];
export type Tag = components["schemas"]["Tag"];
export type Pending = components["schemas"]["Pending"];
export type Uploaded = components["schemas"]["Uploaded"];

export class ApiError extends Error {}

/** Runs an API call and routes its failure to the app's error toast. */
export type Run = (action: () => Promise<unknown>) => void;

function parseError(data: unknown, status: number) {
  const message = (data as { error?: string } | null)?.error;
  return new ApiError(message ?? `Ошибка запроса (${status})`);
}

/** Call the jukebox API. The X-Jukebox header is the server's CSRF guard for every mutating request. */
export async function request<T = { ok: boolean }>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    method,
    headers: { "Content-Type": "application/json", "X-Jukebox": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw parseError(data, response.status);
  return data as T;
}

/** Raw file upload with progress; fetch cannot report upload progress, so this is an XMLHttpRequest. */
export function upload(file: File, onProgress: (fraction: number) => void): Promise<Uploaded> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("X-Jukebox", "1");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new ApiError("Нет связи с Raspberry Pi"));
    xhr.onload = () => {
      let data: unknown = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* non-JSON body: the generic message below */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as Uploaded);
      else reject(parseError(data, xhr.status));
    };
    xhr.send(file);
  });
}

export const api = {
  play: (track: string) => request("POST", "play", { track }),
  stop: () => request("POST", "stop"),
  seek: (position: number) => request("POST", "seek", { position }),
  bind: (track: string) => request("POST", "bind", { track }),
  confirm: (uid: string, track: string) => request("POST", "confirm", { uid, track }),
  cancel: () => request("POST", "cancel"),
  unbind: (uid: string) => request("POST", "unbind", { uid }),
  settings: (settings: Settings) => request("POST", "settings", settings),
  setLabel: (uid: string, label: string) => request("PUT", `tags/${encodeURIComponent(uid)}`, { label }),
  rename: (track: string, title: string) => request("PUT", `tracks/${encodeURIComponent(track)}`, { title }),
  deleteTrack: (track: string) => request("DELETE", `tracks/${encodeURIComponent(track)}`),
  upload,
};

/** The disc's name for the UI: its label when it has one, otherwise the UID. */
export function tagName(status: Status, uid: string) {
  return status.tags[uid]?.label || uid;
}

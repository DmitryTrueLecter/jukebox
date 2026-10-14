import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Pending, type Status } from "./api/client";

export type Connection = "connecting" | "online" | "offline";

/** Live status from /api/events (server-sent events); the browser reconnects on its own. */
export function useStatus() {
  const [status, setStatus] = useState<Status | null>(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const [connection, setConnection] = useState<Connection>("connecting");

  useEffect(() => {
    const source = new EventSource("/api/events");
    source.onopen = () => setConnection("online");
    source.onerror = () => setConnection("offline");
    source.onmessage = (event) => {
      setStatus(JSON.parse(event.data) as Status);
      setReceivedAt(performance.now());
      setConnection("online");
    };
    return () => source.close();
  }, []);

  return { status, receivedAt, connection };
}

export interface Playback {
  /** Extrapolated between server updates so the progress bar moves smoothly. */
  position: number | null;
  duration: number | null;
  playing: boolean;
  seek: (position: number) => Promise<unknown>;
}

/**
 * Playback position for the UI. Between events the position runs on the client clock; after a seek the
 * seek target is used until the next status event, because the server does not push one for a seek.
 */
export function usePlayback(status: Status | null, receivedAt: number): Playback {
  const [now, setNow] = useState(performance.now());
  const [override, setOverride] = useState<{ position: number; at: number; receivedAt: number } | null>(null);
  const playing = status?.playing_track != null && status.position != null;

  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => setNow(performance.now()), 250);
    return () => clearInterval(timer);
  }, [playing]);

  const seek = useCallback(
    async (position: number) => {
      await api.seek(position);
      setOverride({ position, at: performance.now(), receivedAt });
    },
    [receivedAt],
  );

  const duration = status?.duration ?? null;
  if (!playing || status?.position == null) return { position: null, duration, playing: false, seek };
  const base = override && override.receivedAt === receivedAt
    ? override
    : { position: status.position, at: receivedAt };
  const position = base.position + Math.max(0, now - base.at) / 1000;
  return { position: duration != null ? Math.min(position, duration) : position, duration, playing: true, seek };
}

/**
 * Right after a disc is bound, ask for its name: binding is the moment the disc is in hand.
 * Returns the freshly bound disc (without a label yet) and a dismiss function.
 */
export function useLabelPrompt(status: Status | null) {
  const previous = useRef<Pending | null>(null);
  const [prompt, setPrompt] = useState<{ uid: string; track: string } | null>(null);
  useEffect(() => {
    const was = previous.current;
    previous.current = status?.pending ?? null;
    if (!status || !was || status.pending) return;
    const uid = status.current_tag;
    if (uid && status.tags[uid]?.track === was.track && !status.tags[uid]?.label) {
      setPrompt({ uid, track: was.track });
    }
  }, [status]);
  return { prompt, dismiss: () => setPrompt(null) };
}

/**
 * Volume slider value that follows the user's hand, not the server: the local value wins until the
 * server echoes it back, so an event with a stale volume does not drag the slider backwards.
 */
export function useVolume(status: Status | null, run: (action: () => Promise<unknown>) => void) {
  const [local, setLocal] = useState<number | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const serverVolume = status?.settings.volume ?? 0;
  useEffect(() => {
    if (local != null && serverVolume === local) setLocal(null);
  }, [serverVolume, local]);

  const setVolume = (volume: number) => {
    if (!status) return;
    setLocal(volume);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => run(() => api.settings({ ...status.settings, volume })), 150);
  };
  return { volume: local ?? serverVolume, setVolume };
}

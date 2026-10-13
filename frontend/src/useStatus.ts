import { useEffect, useState } from "react";
import type { Status } from "./api/client";

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

/** Position extrapolated between server updates so the progress bar moves smoothly. */
export function usePosition(status: Status | null, receivedAt: number) {
  const [now, setNow] = useState(performance.now());
  const playing = status?.playing_track != null && status.position != null;
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => setNow(performance.now()), 250);
    return () => clearInterval(timer);
  }, [playing]);
  if (!playing || status?.position == null) return null;
  const position = status.position + Math.max(0, now - receivedAt) / 1000;
  return status.duration != null ? Math.min(position, status.duration) : position;
}
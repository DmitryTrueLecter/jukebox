import { useState } from "react";
import { api, ApiError } from "./api/client";
import { usePosition, useStatus } from "./useStatus";

function formatTime(seconds: number | null | undefined) {
  if (seconds == null) return "–:––";
  const s = Math.floor(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export default function App() {
  const { status, receivedAt, connection } = useStatus();
  const position = usePosition(status, receivedAt);
  const [error, setError] = useState<string | null>(null);

  const run = (action: () => Promise<unknown>) =>
    action().then(() => setError(null), (e) => setError(e instanceof ApiError ? e.message : String(e)));

  if (!status) return <main className="app"><p className="muted">Подключение…</p></main>;

  const tracks = Object.entries(status.tracks);
  const tagsByTrack = new Map<string, string[]>();
  for (const [uid, track] of Object.entries(status.tags)) {
    tagsByTrack.set(track, [...(tagsByTrack.get(track) ?? []), uid]);
  }
  const playing = status.playing_track;

  return (
    <main className="app">
      <header className="header">
        <h1>Jukebox</h1>
        <span className={`connection ${connection}`}>{connection === "online" ? "на связи" : "нет связи"}</span>
      </header>
      <p className="message">{status.message}</p>
      {(error || status.reader_error || status.audio_error) && (
        <p className="error">{error ?? status.audio_error ?? status.reader_error}</p>
      )}
      <table className="tracks">
        <thead><tr><th /><th>Трек</th><th>Метки</th></tr></thead>
        <tbody>
          {tracks.map(([id, track]) => (
            <tr key={id} className={id === playing ? "active" : undefined}>
              <td>
                <button onClick={() => run(id === playing ? api.stop : () => api.play(id))}>
                  {id === playing ? "■" : "▶"}
                </button>
              </td>
              <td>{track.title}</td>
              <td className="muted">{(tagsByTrack.get(id) ?? []).join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {playing && (
        <footer className="player">
          <button onClick={() => run(api.stop)}>■</button>
          <span>{status.tracks[playing]?.title}</span>
          <span className="muted">{formatTime(position)} / {formatTime(status.duration)}</span>
        </footer>
      )}
    </main>
  );
}
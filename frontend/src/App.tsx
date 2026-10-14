import { useEffect, useState } from "react";
import { ApiError, type Run } from "./api/client";
import LabelDialog from "./components/LabelDialog";
import PlayerBar from "./components/PlayerBar";
import SettingsPanel from "./components/SettingsPanel";
import StatusStrip from "./components/StatusStrip";
import TrackList from "./components/TrackList";
import Upload from "./components/Upload";
import { plural } from "./format";
import { useLabelPrompt, usePlayback, useStatus, useVolume } from "./useStatus";

export default function App() {
  const { status, receivedAt, connection } = useStatus();
  const playback = usePlayback(status, receivedAt);
  const [error, setError] = useState<string | null>(null);
  const run: Run = (action) =>
    action().then(() => setError(null), (e) => setError(e instanceof ApiError ? e.message : String(e)));
  const { volume, setVolume } = useVolume(status, run);
  const { prompt, dismiss } = useLabelPrompt(status);

  useEffect(() => {
    if (!error) return;
    const timer = setTimeout(() => setError(null), 6000);
    return () => clearTimeout(timer);
  }, [error]);

  if (!status) {
    return (
      <main className="app loading">
        <p className="muted">{connection === "offline" ? "Нет связи с Raspberry Pi, переподключение…" : "Подключение…"}</p>
      </main>
    );
  }

  const discs = Object.values(status.tags).filter((tag) => tag.track).length;

  return (
    <main className="app">
      <header className="header">
        <div>
          <div className="eyebrow">NFC music player</div>
          <h1>Jukebox</h1>
        </div>
        <div className="header-side">
          <span className="muted small">
            {plural(Object.keys(status.tracks).length, "трек", "трека", "треков")} · {plural(discs, "диск", "диска", "дисков")}
          </span>
          <span className={`connection ${connection}`}>{connection === "online" ? "на связи" : "нет связи"}</span>
        </div>
      </header>
      <StatusStrip status={status} connection={connection} run={run} />
      <div className="columns">
        <TrackList status={status} playback={playback} run={run} />
        <aside className="sidebar">
          <Upload />
          <SettingsPanel status={status} volume={volume} setVolume={setVolume} run={run} />
        </aside>
      </div>
      <PlayerBar status={status} playback={playback} volume={volume} setVolume={setVolume} run={run} />
      {prompt && <LabelDialog uid={prompt.uid} track={prompt.track} status={status} run={run} onClose={dismiss} />}
      {error && (
        <div className="toast" role="alert" onClick={() => setError(null)}>
          {error}
        </div>
      )}
    </main>
  );
}

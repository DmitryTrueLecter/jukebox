import { api, tagName, type Run, type Status } from "../api/client";
import type { Playback } from "../useStatus";
import SeekBar from "./SeekBar";

interface Props {
  status: Status;
  playback: Playback;
  volume: number;
  setVolume: (volume: number) => void;
  run: Run;
}

/** Sticky bottom bar: what is playing, seek, stop, volume. Always visible so the layout never jumps. */
export default function PlayerBar({ status, playback, volume, setVolume, run }: Props) {
  const id = status.playing_track;
  const track = id ? status.tracks[id] : null;
  const subtitle = !track
    ? "ничего не играет"
    : status.source === "tag" && status.current_tag
      ? `с диска «${tagName(status, status.current_tag)}»`
      : "запущен кнопкой, один раз";

  return (
    <footer className={`player ${track ? "active" : ""}`}>
      <div className="player-info">
        <div className="player-title">{track?.title ?? "Jukebox"}</div>
        <div className="muted small">{subtitle}</div>
      </div>
      <div className="player-center">
        <button className={`play ${track ? "stop" : ""}`} disabled={!track} title="Стоп" onClick={() => run(api.stop)}>
          ■
        </button>
        <SeekBar position={playback.position} duration={playback.duration} onSeek={(p) => run(() => playback.seek(p))} disabled={!track} />
      </div>
      <label className="player-volume" title={`Громкость ${volume}%`}>
        <span aria-hidden="true">{volume === 0 ? "🔇" : volume < 50 ? "🔉" : "🔊"}</span>
        <input type="range" min={0} max={100} value={volume} aria-label="Громкость" onChange={(event) => setVolume(Number(event.target.value))} />
      </label>
    </footer>
  );
}

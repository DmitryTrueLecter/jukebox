import { useState } from "react";
import { api, type Run, type Status, type Track } from "../api/client";
import { formatTime } from "../format";
import type { Playback } from "../useStatus";
import InlineEdit from "./InlineEdit";
import SeekBar from "./SeekBar";
import SoundEditor from "./SoundEditor";

interface Props {
  id: string;
  track: Track;
  tags: string[];
  status: Status;
  playback: Playback;
  run: Run;
}

export default function TrackRow({ id, track, tags, status, playback, run }: Props) {
  const [editing, setEditing] = useState<"title" | string | null>(null); // "title" or a tag uid
  const [sound, setSound] = useState(false);
  const playing = status.playing_track === id;
  const analyzing = status.analysis.running === id;
  const queued = status.analysis.queue.includes(id);
  const busy = Boolean(status.pending);

  const remove = () => {
    const discs = tags.length ? " Диски останутся без трека." : "";
    if (window.confirm(`Удалить «${track.title}»? Файл будет стёрт с Raspberry Pi.${discs}`)) {
      run(() => api.deleteTrack(id));
    }
  };
  const unbind = (uid: string) => {
    const name = status.tags[uid]?.label || uid;
    if (window.confirm(`Отвязать диск «${name}» от «${track.title}»?`)) run(() => api.unbind(uid));
  };

  return (
    <li className={`track ${playing ? "playing" : ""}`}>
      <div className="track-main">
        <button
          className={`play ${playing ? "stop" : ""}`}
          disabled={busy}
          title={playing ? "Стоп" : "Плей"}
          onClick={() => run(playing ? api.stop : () => api.play(id))}
        >
          {playing ? "■" : "▶"}
        </button>
        <div className="track-body">
          {editing === "title" ? (
            <InlineEdit
              value={track.title}
              maxLength={240}
              onSave={(title) => { setEditing(null); run(() => api.rename(id, title)); }}
              onCancel={() => setEditing(null)}
            />
          ) : (
            <div className="track-title" onDoubleClick={() => setEditing("title")} title="Двойной клик — переименовать">
              {track.title}
              {analyzing && <span className="badge">анализируется…</span>}
              {queued && !analyzing && <span className="badge quiet">в очереди на анализ</span>}
              {track.analysis_error && !analyzing && !queued && (
                <button className="badge bad" title={`${track.analysis_error}. Нажми, чтобы открыть и повторить`} onClick={() => setSound(true)}>
                  ошибка анализа
                </button>
              )}
              {track.duration != null && (
                <span className="muted small duration">{formatTime((track.trim_end ?? track.duration) - (track.trim_start ?? 0))}</span>
              )}
            </div>
          )}
          <div className="tags">
            {tags.map((uid) => {
              const tag = status.tags[uid];
              const inReader = status.current_tag === uid;
              return editing === uid ? (
                <InlineEdit
                  key={uid}
                  value={tag?.label ?? ""}
                  placeholder="Название диска"
                  maxLength={80}
                  onSave={(label) => { setEditing(null); run(() => api.setLabel(uid, label)); }}
                  onCancel={() => setEditing(null)}
                />
              ) : (
                <span key={uid} className={`tag ${inReader ? "in-reader" : ""}`} title={`Метка ${uid}${inReader ? " · в считывателе" : ""}`}>
                  <button className="tag-name" onClick={() => setEditing(uid)} title="Переименовать диск">
                    {tag?.label || <code>{uid}</code>}
                  </button>
                  <button className="tag-remove" onClick={() => unbind(uid)} title="Отвязать диск">×</button>
                </span>
              );
            })}
            <button
              className="tag tag-add"
              disabled={busy || Boolean(status.reader_error)}
              onClick={() => run(() => api.bind(id))}
              title={status.reader_error ? "Считыватель недоступен" : "Привязать новый диск к этому треку"}
            >
              + диск
            </button>
          </div>
        </div>
        <div className="track-actions">
          <button className={`text ${sound ? "on" : ""}`} onClick={() => setSound(!sound)} title="Громкость, обрезка и анализ трека">Звук</button>
          <button className="icon" onClick={() => setEditing("title")} title="Переименовать">✎</button>
          <button className="icon danger" onClick={remove} title="Удалить трек">🗑</button>
        </div>
      </div>
      {playing && (
        <div className="track-player">
          <SeekBar position={playback.position} duration={playback.duration} onSeek={(p) => run(() => playback.seek(p))} />
        </div>
      )}
      {sound && <SoundEditor id={id} track={track} status={status} playback={playback} run={run} />}
    </li>
  );
}

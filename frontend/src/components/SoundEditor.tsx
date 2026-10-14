import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type Run, type Status, type Track, type Waveform as WaveformData } from "../api/client";
import { formatTime } from "../format";
import type { Playback } from "../useStatus";
import Waveform from "./Waveform";

interface Props {
  id: string;
  track: Track;
  status: Status;
  playback: Playback;
  run: Run;
}

const db = (value: number) => `${value > 0 ? "+" : ""}${value.toFixed(1)} дБ`;

/** Per-track sound: analysis results, loudness offset, and the non-destructive trim on a waveform. */
export default function SoundEditor({ id, track, status, playback, run }: Props) {
  const [wave, setWave] = useState<WaveformData | null>(null);
  const [waveError, setWaveError] = useState<string | null>(null);
  const [range, setRange] = useState<{ start: number; end: number } | null>(null); // while dragging
  const [offset, setOffset] = useState<number | null>(null); // while sliding
  const offsetTimer = useRef<number | undefined>(undefined);

  const running = status.analysis.running === id;
  const analyzed = track.analyzed_at != null;

  useEffect(() => {
    if (!analyzed) return;
    let cancelled = false;
    api.waveform(id).then(
      (data) => { if (!cancelled) { setWave(data); setWaveError(null); } },
      (e) => { if (!cancelled) setWaveError(e instanceof ApiError ? e.message : String(e)); },
    );
    return () => { cancelled = true; };
  }, [id, analyzed, track.analyzed_at]);

  const duration = wave?.duration ?? track.duration ?? 0;
  const start = range?.start ?? track.trim_start ?? 0;
  const end = range?.end ?? track.trim_end ?? duration;
  const trimmed = (track.trim_start ?? 0) > 0 || track.trim_end != null;
  const suggestion = wave && (wave.silence_start != null || wave.silence_end != null)
    ? { start: wave.silence_start ?? 0, end: wave.silence_end ?? duration }
    : null;
  const suggestionApplied = suggestion != null
    && Math.abs(suggestion.start - (track.trim_start ?? 0)) < 0.05
    && Math.abs(suggestion.end - (track.trim_end ?? duration)) < 0.05;

  const commitTrim = useCallback((s: number, e: number) => {
    setRange(null);
    const clearsAll = s <= 0.005 && e >= duration - 0.05;
    run(() => api.updateTrack(id, clearsAll ? { trim_start: null, trim_end: null } : { trim_start: s, trim_end: e }));
  }, [duration, id, run]);
  const onDrag = useCallback((s: number, e: number) => setRange({ start: s, end: e }), []);
  const audition = useCallback((position: number) => run(() => api.play(id, position)), [id, run]);

  const changeOffset = (value: number) => {
    setOffset(value);
    window.clearTimeout(offsetTimer.current);
    offsetTimer.current = window.setTimeout(() => {
      run(() => api.updateTrack(id, { gain_offset_db: value }).then(() => setOffset(null)));
    }, 200);
  };
  const shownOffset = offset ?? track.gain_offset_db;
  const total = track.gain_db + shownOffset;
  const playing = status.playing_track === id;
  const cursor = playing && playback.position != null ? playback.position + (track.trim_start ?? 0) : null;

  const timeInput = (which: "start" | "end") => (
    <input
      type="number"
      className="trim-time"
      min={0}
      max={duration}
      step={0.1}
      value={(which === "start" ? start : end).toFixed(1)}
      onChange={(event) => {
        const value = Number(event.target.value);
        if (!Number.isFinite(value)) return;
        if (which === "start") commitTrim(Math.min(value, end - 0.5), end);
        else commitTrim(start, Math.max(value, start + 0.5));
      }}
    />
  );

  return (
    <div className="sound">
      <div className="sound-head">
        {running ? (
          <span className="muted">Анализируется…</span>
        ) : track.analysis_error ? (
          <span className="error" title={track.analysis_error}>Ошибка анализа: {track.analysis_error}</span>
        ) : analyzed ? (
          <span className="muted small">
            Громкость {track.loudness_lufs?.toFixed(1) ?? "—"} LUFS · пик {track.true_peak_db?.toFixed(1) ?? "—"} дБ
            {" · "}авто {db(track.gain_db)}{!status.settings.normalize && " (выравнивание выключено)"}
          </span>
        ) : (
          <span className="muted">Ещё не проанализирован{status.analysis.queued ? " (в очереди)" : ""}</span>
        )}
        <button className="link" disabled={running} onClick={() => run(() => api.analyze({ track: id }))}>
          {analyzed ? "Проанализировать заново" : "Проанализировать"}
        </button>
      </div>

      <label className="field offset">
        <span>Поправка громкости: <b>{db(shownOffset)}</b> <span className="muted">(итого {db(total)})</span></span>
        <input type="range" min={-12} max={12} step={0.5} value={shownOffset} onChange={(event) => changeOffset(Number(event.target.value))} />
      </label>

      {wave ? (
        <>
          <Waveform data={wave} start={start} end={end} cursor={cursor} onChange={onDrag} onCommit={commitTrim} onAudition={audition} />
          <div className="trim-row">
            <span className="muted small">Начало</span>
            {timeInput("start")}
            <span className="muted small">Конец</span>
            {timeInput("end")}
            <span className="muted small">
              звучит {formatTime(end - start)} из {formatTime(duration)} · клик по волне — слушать отсюда
            </span>
            <span className="trim-actions">
              {suggestion && !suggestionApplied && (
                <button className="small" onClick={() => commitTrim(suggestion.start, suggestion.end)}
                  title={`Тишина в начале до ${formatTime(suggestion.start)}, в конце с ${formatTime(suggestion.end)}`}>
                  Обрезать тишину
                </button>
              )}
              {trimmed && <button className="small" onClick={() => commitTrim(0, duration)}>Сбросить обрезку</button>}
            </span>
          </div>
        </>
      ) : waveError ? (
        <p className="error small">{waveError}</p>
      ) : analyzed ? (
        <p className="muted small">Загрузка волны…</p>
      ) : (
        <p className="muted small">Обрезка появится после анализа.</p>
      )}
    </div>
  );
}

import { useEffect, useState } from "react";
import { api, type Run, type Status } from "../api/client";

interface Props {
  status: Status;
  volume: number;
  setVolume: (volume: number) => void;
  run: Run;
}

/** Playback settings. Everything applies immediately; the grace period is sent on blur or Enter. */
export default function SettingsPanel({ status, volume, setVolume, run }: Props) {
  const settings = status.settings;
  const [grace, setGrace] = useState(String(settings.read_grace_ms));
  useEffect(() => setGrace(String(settings.read_grace_ms)), [settings.read_grace_ms]);

  const saveGrace = () => {
    const value = Number(grace);
    if (!Number.isInteger(value) || value < 1 || value > 60000) {
      setGrace(String(settings.read_grace_ms));
      return;
    }
    if (value !== settings.read_grace_ms) run(() => api.settings({ ...settings, read_grace_ms: value }));
  };

  return (
    <section className="settings">
      <h2>Воспроизведение</h2>
      <label className="field">
        <span>Громкость: <b>{volume}%</b></span>
        <input type="range" min={0} max={100} value={volume} onChange={(event) => setVolume(Number(event.target.value))} />
      </label>
      <label className="field">
        <span>Защита от пропусков чтения, мс</span>
        <input
          type="number"
          min={1}
          max={60000}
          step={1}
          value={grace}
          onChange={(event) => setGrace(event.target.value)}
          onBlur={saveGrace}
          onKeyDown={(event) => { if (event.key === "Enter") (event.target as HTMLInputElement).blur(); }}
        />
      </label>
      <label className="field check">
        <input
          type="checkbox"
          checked={settings.continue_playback}
          onChange={(event) => run(() => api.settings({ ...settings, continue_playback: event.target.checked }))}
        />
        <span>Продолжать трек с места при повторном поднесении диска</span>
      </label>
      <p className="muted small">
        С диском трек запускается снова после окончания. Без диска кнопка «Плей» запускает его один раз.
        «Стоп» отменяет автоматический повтор, пока диск не вынут.
      </p>
    </section>
  );
}

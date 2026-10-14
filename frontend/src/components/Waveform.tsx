import { useEffect, useRef, useState } from "react";
import type { Waveform as WaveformData } from "../api/client";
import { formatTime } from "../format";

interface Props {
  data: WaveformData;
  start: number; // trim start, seconds
  end: number; // trim end, seconds (duration when not trimmed)
  cursor: number | null; // absolute playback position
  onChange: (start: number, end: number) => void; // called while dragging a handle
  onCommit: (start: number, end: number) => void; // called when the handle is released
  onAudition: (position: number) => void; // click inside the kept range: listen from there
}

const MIN_LENGTH = 0.5;

/** Waveform with two drag handles for the trim range. Everything outside the range is dimmed. */
export default function Waveform({ data, start, end, cursor, onChange, onCommit, onAudition }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<"start" | "end" | null>(null);
  const range = useRef({ start, end });
  range.current = { start, end };
  const { duration } = data;

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const width = element.clientWidth;
    const height = element.clientHeight;
    const scale = window.devicePixelRatio || 1;
    element.width = width * scale;
    element.height = height * scale;
    const ctx = element.getContext("2d");
    if (!ctx) return;
    ctx.scale(scale, scale);
    ctx.clearRect(0, 0, width, height);
    const style = getComputedStyle(element);
    const accent = style.getPropertyValue("--accent").trim() || "#2f6fdf";
    const muted = style.getPropertyValue("--border").trim() || "#ccc";
    const n = data.peaks.length;
    const middle = height / 2;
    for (let i = 0; i < n; i++) {
      const x0 = (i / n) * width;
      const x1 = ((i + 1) / n) * width;
      const t = (i + 0.5) / n * duration;
      const h = Math.max(1, (data.peaks[i] / 255) * (height - 4) / 2);
      ctx.fillStyle = t >= start && t <= end ? accent : muted;
      ctx.fillRect(x0, middle - h, Math.max(1, x1 - x0 - 0.5), h * 2);
    }
  }, [data, duration, start, end]);

  const toTime = (clientX: number) => {
    const rect = box.current!.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    return ratio * duration;
  };

  useEffect(() => {
    if (!drag) return;
    const move = (event: PointerEvent) => {
      const t = toTime(event.clientX);
      const { start: s, end: e } = range.current;
      if (drag === "start") onChange(Math.min(t, e - MIN_LENGTH), e);
      else onChange(s, Math.max(t, s + MIN_LENGTH));
    };
    const up = () => {
      onCommit(range.current.start, range.current.end);
      setDrag(null);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [drag, onChange, onCommit]);

  const pct = (t: number) => `${(t / duration) * 100}%`;

  return (
    <div
      ref={box}
      className={`wave ${drag ? "dragging" : ""}`}
      onClick={(event) => {
        if (drag) return;
        const t = toTime(event.clientX);
        if (t >= start && t <= end) onAudition(t - start);
      }}
    >
      <canvas ref={canvas} />
      <div className="wave-shade" style={{ left: 0, width: pct(start) }} />
      <div className="wave-shade" style={{ left: pct(end), right: 0 }} />
      {cursor != null && <div className="wave-cursor" style={{ left: pct(Math.min(cursor, duration)) }} />}
      <div
        className="wave-handle start"
        style={{ left: pct(start) }}
        title={`Начало: ${formatTime(start)}`}
        onPointerDown={(event) => { event.preventDefault(); event.stopPropagation(); setDrag("start"); }}
        onClick={(event) => event.stopPropagation()}
      />
      <div
        className="wave-handle end"
        style={{ left: pct(end) }}
        title={`Конец: ${formatTime(end)}`}
        onPointerDown={(event) => { event.preventDefault(); event.stopPropagation(); setDrag("end"); }}
        onClick={(event) => event.stopPropagation()}
      />
    </div>
  );
}

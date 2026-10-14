import { useRef, useState } from "react";
import { formatTime } from "../format";

interface Props {
  position: number | null;
  duration: number | null;
  onSeek: (position: number) => void;
  disabled?: boolean;
}

const STEPS = 1000;

/** Progress bar that is also a seek control. While dragging it shows the hand, not the server. */
export default function SeekBar({ position, duration, onSeek, disabled }: Props) {
  const [drag, setDrag] = useState<number | null>(null);
  const dragging = useRef(false);
  const usable = !disabled && duration != null && duration > 0;
  const ratio = usable && position != null ? Math.min(position, duration) / duration : 0;
  const value = drag ?? Math.round(ratio * STEPS);
  const shown = drag != null && duration ? (drag / STEPS) * duration : position;

  const commit = (step: number) => {
    if (duration) onSeek((step / STEPS) * duration);
  };
  const release = () => {
    if (drag != null) commit(drag);
    dragging.current = false;
    setDrag(null);
  };

  return (
    <div className="seek">
      <span className="time">{formatTime(usable ? shown : null)}</span>
      <input
        type="range"
        min={0}
        max={STEPS}
        value={value}
        disabled={!usable}
        aria-label="Позиция"
        onPointerDown={() => {
          dragging.current = true;
          setDrag(value);
        }}
        onChange={(event) => {
          const step = Number(event.target.value);
          if (dragging.current) setDrag(step);
          else commit(step); // keyboard
        }}
        onPointerUp={release}
        onPointerCancel={release}
      />
      <span className="time">{formatTime(usable ? duration : null)}</span>
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { api, type Run, type Status } from "../api/client";

interface Props {
  uid: string;
  track: string;
  status: Status;
  run: Run;
  onClose: () => void;
}

/** Shown right after a disc has been bound: name it while it is still in hand. */
export default function LabelDialog({ uid, track, status, run, onClose }: Props) {
  const [label, setLabel] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);

  const save = () => {
    if (label.trim()) run(() => api.setLabel(uid, label));
    onClose();
  };

  return (
    <div className="backdrop" onClick={onClose}>
      <form className="dialog" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); save(); }}>
        <h2>Диск привязан</h2>
        <p className="muted">
          Метка <code>{uid}</code> теперь запускает «{status.tracks[track]?.title ?? ""}». Как назвать этот диск?
        </p>
        <input ref={input} value={label} maxLength={80} placeholder="Например: красно-жёлтый" onChange={(event) => setLabel(event.target.value)} />
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>Без названия</button>
          <button type="submit" className="primary" disabled={!label.trim()}>Сохранить</button>
        </div>
      </form>
    </div>
  );
}

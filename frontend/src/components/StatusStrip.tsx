import { api, tagName, type Run, type Status } from "../api/client";
import type { Connection } from "../useStatus";

interface Props {
  status: Status;
  connection: Connection;
  run: Run;
}

/** What the jukebox is doing right now: the engine message, the binding flow, errors. */
export default function StatusStrip({ status, connection, run }: Props) {
  const pending = status.pending;
  const target = pending ? status.tracks[pending.track]?.title ?? "" : "";
  let message = status.message;
  if (pending?.phase === "conflict" && pending.uid) {
    const old = (pending.old_track && status.tracks[pending.old_track]?.title) || "другой трек";
    message = `Диск «${tagName(status, pending.uid)}» уже играет «${old}». Заменить на «${target}»?`;
  }
  const errors = [status.reader_error && `Считыватель: ${status.reader_error}`, status.audio_error && `Звук: ${status.audio_error}`]
    .filter(Boolean) as string[];

  return (
    <section className={`status ${pending ? "status-binding" : ""}`} aria-live="polite">
      <div className="status-main">
        <p className="status-message">{message}</p>
        <p className="muted small">
          {pending && pending.phase !== "conflict" && <>Привязка: «{target}» · </>}
          {status.current_tag
            ? <>Диск в считывателе: {tagName(status, status.current_tag)}</>
            : status.last_tag
              ? <>Последний диск: {tagName(status, status.last_tag)}</>
              : "Диск ещё не обнаружен"}
        </p>
        {connection === "offline" && <p className="error">Нет связи с Raspberry Pi, переподключение…</p>}
        {errors.map((text) => <p key={text} className="error">{text}</p>)}
      </div>
      {pending && (
        <div className="status-actions">
          {pending.phase === "conflict" && pending.uid && (
            <button className="primary" onClick={() => run(() => api.confirm(pending.uid!, pending.track))}>
              Заменить
            </button>
          )}
          <button onClick={() => run(api.cancel)}>Отменить привязку</button>
        </div>
      )}
    </section>
  );
}

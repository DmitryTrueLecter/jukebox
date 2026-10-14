import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";

const ACCEPT = ".mp3,.flac,.wav,.ogg,.m4a,.aac,.opus";

interface Item {
  name: string;
  progress: number;
  state: "queued" | "uploading" | "done" | "duplicate" | "error";
  error?: string;
}

/** File picker + drop zone. Files go up one at a time; the library updates itself through SSE. */
export default function Upload() {
  const [items, setItems] = useState<Item[]>([]);
  const [over, setOver] = useState(false);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const count = useRef(0); // items ever added; indexes stay stable because the list is only cleared when idle

  const patch = (index: number, change: Partial<Item>) =>
    setItems((list) => list.map((item, i) => (i === index ? { ...item, ...change } : item)));

  const add = (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    const start = count.current;
    count.current += list.length;
    setItems((existing) => [...existing, ...list.map((file) => ({ name: file.name, progress: 0, state: "queued" as const }))]);
    list.forEach((file, offset) => {
      const index = start + offset;
      queue.current = queue.current.then(async () => {
        patch(index, { state: "uploading" });
        try {
          const result = await api.upload(file, (progress) => patch(index, { progress }));
          patch(index, { state: result.duplicate ? "duplicate" : "done", progress: 1 });
        } catch (error) {
          patch(index, { state: "error", error: error instanceof ApiError ? error.message : String(error) });
        }
      });
    });
  };
  const clear = () => {
    count.current = 0;
    setItems([]);
  };

  const finished = items.length > 0 && items.every((item) => item.state !== "queued" && item.state !== "uploading");

  return (
    <section
      className={`upload ${over ? "over" : ""}`}
      onDragOver={(event) => { event.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => { event.preventDefault(); setOver(false); add(event.dataTransfer.files); }}
    >
      <h2>Добавить музыку</h2>
      <p className="muted small">Перетащи файлы сюда или выбери их. До 200 МБ на файл: MP3, FLAC, WAV, OGG, M4A, AAC, OPUS.</p>
      <label className="button primary">
        Выбрать файлы
        <input type="file" multiple accept={ACCEPT} hidden onChange={(event) => { add(event.target.files ?? []); event.target.value = ""; }} />
      </label>
      {items.length > 0 && (
        <ul className="uploads">
          {items.map((item, index) => (
            <li key={index} className={`upload-item ${item.state}`}>
              <span className="upload-name">{item.name}</span>
              <span className="upload-state">
                {item.state === "queued" && "в очереди"}
                {item.state === "uploading" && `${Math.round(item.progress * 100)}%`}
                {item.state === "done" && "загружено"}
                {item.state === "duplicate" && "уже в библиотеке"}
                {item.state === "error" && (item.error ?? "ошибка")}
              </span>
              {item.state === "uploading" && <progress value={item.progress} max={1} />}
            </li>
          ))}
        </ul>
      )}
      {finished && <button className="link" onClick={clear}>Очистить список</button>}
    </section>
  );
}

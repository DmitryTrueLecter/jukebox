import { useMemo, useState } from "react";
import { api, type Run, type Status } from "../api/client";
import type { Playback } from "../useStatus";
import TrackRow from "./TrackRow";

interface Props {
  status: Status;
  playback: Playback;
  run: Run;
}

type Sort = "title" | "newest";

export default function TrackList({ status, playback, run }: Props) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("title");

  const tagsByTrack = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const [uid, tag] of Object.entries(status.tags)) {
      if (tag.track) map.set(tag.track, [...(map.get(tag.track) ?? []), uid]);
    }
    return map;
  }, [status.tags]);

  const needle = query.trim().toLocaleLowerCase();
  const tracks = Object.entries(status.tracks)
    .filter(([id, track]) => {
      if (!needle) return true;
      const names = (tagsByTrack.get(id) ?? []).flatMap((uid) => [uid, status.tags[uid]?.label ?? ""]);
      return [track.title, ...names].join(" ").toLocaleLowerCase().includes(needle);
    })
    .sort(([, a], [, b]) => (sort === "newest" ? b.created_at - a.created_at : a.title.localeCompare(b.title, "ru")));

  const free = Object.entries(status.tags).filter(([, tag]) => !tag.track && tag.label);

  return (
    <section className="library">
      <div className="toolbar">
        <input
          type="search"
          placeholder="Поиск по названию или диску"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select value={sort} onChange={(event) => setSort(event.target.value as Sort)} aria-label="Сортировка">
          <option value="title">По названию</option>
          <option value="newest">Сначала новые</option>
        </select>
      </div>
      {tracks.length ? (
        <ul className="tracks">
          {tracks.map(([id, track]) => (
            <TrackRow key={id} id={id} track={track} tags={tagsByTrack.get(id) ?? []} status={status} playback={playback} run={run} />
          ))}
        </ul>
      ) : (
        <p className="muted empty">{Object.keys(status.tracks).length ? "Ничего не найдено" : "Загрузи первые треки, они появятся здесь."}</p>
      )}
      {free.length > 0 && (
        <div className="free-discs">
          <span className="muted small">Диски без трека:</span>
          {free.map(([uid, tag]) => (
            <span key={uid} className="tag" title={`Метка ${uid}. Привяжи через «+ диск» у нужного трека.`}>
              <span className="tag-name">{tag.label}</span>
              <button
                className="tag-remove"
                title="Забыть диск"
                onClick={() => {
                  if (window.confirm(`Забыть диск «${tag.label}»?`)) run(() => api.setLabel(uid, ""));
                }}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

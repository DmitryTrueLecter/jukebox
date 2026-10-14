import { useEffect, useRef, useState } from "react";

interface Props {
  value: string;
  placeholder?: string;
  maxLength?: number;
  onSave: (value: string) => Promise<unknown> | void;
  onCancel: () => void;
}

/** A text input that commits on Enter or blur and cancels on Escape. */
export default function InlineEdit({ value, placeholder, maxLength, onSave, onCancel }: Props) {
  const [text, setText] = useState(value);
  const input = useRef<HTMLInputElement>(null);
  const done = useRef(false);
  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, []);

  const finish = (save: boolean) => {
    if (done.current) return;
    done.current = true;
    if (save && text.trim() !== value) void onSave(text);
    else onCancel();
  };

  return (
    <input
      ref={input}
      className="inline-edit"
      value={text}
      placeholder={placeholder}
      maxLength={maxLength}
      onChange={(event) => setText(event.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(event) => {
        if (event.key === "Enter") finish(true);
        if (event.key === "Escape") finish(false);
      }}
    />
  );
}

"use client";

import { useEffect, useId, useRef, useState } from "react";

import { Icon } from "./icons";  // chevron and check icons

/** One choice in a Select: its value, label and optional hint. */
export interface SelectOption<T extends string | number> {
  value: T;
  label: string;
  /** A short line under the label, for options that need explaining. */
  hint?: string;
}

/**
 * A dropdown that looks the same everywhere.
 *
 * The browser's own <select> popup is drawn by the operating system, so it ignores
 * the page's colours and shows up grey or dark on top of a light page. This one is
 * a listbox the page draws itself, with the keyboard behaviour people expect:
 * arrows to move, Enter to choose, Escape to close, Home and End to jump.
 */
export function Select<T extends string | number>({
  id,
  value,
  onChange,
  options,
  disabled = false,
  "aria-label": ariaLabel,
}: {
  id?: string;
  value: T;
  onChange: (value: T) => void;
  options: SelectOption<T>[];
  disabled?: boolean;
  "aria-label"?: string;
}) {
  const autoId = useId();
  const listId = `${autoId}-list`;
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  const selectedIndex = Math.max(
    0,
    options.findIndex((o) => o.value === value),
  );
  const current = options[selectedIndex];

  // Close the list when the user clicks outside it.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  // Keep the highlighted option in view in a long list.
  useEffect(() => {
    if (!open) return;
    listRef.current?.children[active]?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  /** Open the list with the current choice highlighted. */
  function show() {
    setActive(selectedIndex);
    setOpen(true);
  }

  /** Pick an option and close the list. */
  function choose(index: number) {
    onChange(options[index].value);
    setOpen(false);
  }

  /** Keyboard support: arrows, Home/End, Enter/Space, Escape and Tab. */
  function onKeyDown(event: React.KeyboardEvent) {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!open) show();
        else setActive((i) => Math.min(options.length - 1, i + 1));
        break;
      case "ArrowUp":
        event.preventDefault();
        if (!open) show();
        else setActive((i) => Math.max(0, i - 1));
        break;
      case "Home":
        if (open) {
          event.preventDefault();
          setActive(0);
        }
        break;
      case "End":
        if (open) {
          event.preventDefault();
          setActive(options.length - 1);
        }
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        if (open) choose(active);
        else show();
        break;
      case "Escape":
        if (open) {
          event.preventDefault();
          setOpen(false);
        }
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  }

  return (
    <div className={`select ${open ? "select--open" : ""}`} ref={wrapRef}>
      <button
        type="button"
        id={id}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        aria-label={ariaLabel}
        aria-activedescendant={open ? `${autoId}-opt-${active}` : undefined}
        className="select-trigger"
        disabled={disabled}
        onClick={() => (open ? setOpen(false) : show())}
        onKeyDown={onKeyDown}
      >
        <span>{current?.label ?? ""}</span>
        <Icon name="chevron-down" size={16} className="select-chevron" />
      </button>
      {open && (
        <ul className="select-list" role="listbox" id={listId} ref={listRef}>
          {options.map((option, index) => (
            <li
              key={String(option.value)}
              id={`${autoId}-opt-${index}`}
              role="option"
              aria-selected={index === selectedIndex}
              className={`select-option ${index === active ? "select-option--active" : ""} ${
                index === selectedIndex ? "select-option--chosen" : ""
              }`}
              onPointerEnter={() => setActive(index)}
              onClick={() => choose(index)}
            >
              <span>
                {option.label}
                {option.hint && <small>{option.hint}</small>}
              </span>
              {index === selectedIndex && <Icon name="check" size={16} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

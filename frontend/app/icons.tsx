import type { SVGProps } from "react";  // SVG props type

/**
 * One line-icon set for the whole app, drawn here so nothing depends on an icon
 * package or on emoji (which render differently on every system).
 */
export type IconName =
  | "users"
  | "user"
  | "user-plus"
  | "plus"
  | "bot"
  | "clock"
  | "history"
  | "bell"
  | "grid"
  | "mic"
  | "mic-off"
  | "video"
  | "video-off"
  | "volume"
  | "volume-off"
  | "chat"
  | "settings"
  | "phone-off"
  | "copy"
  | "check"
  | "check-circle"
  | "trophy"
  | "download"
  | "arrow-right"
  | "arrow-left"
  | "shuffle"
  | "lock"
  | "calendar"
  | "chart"
  | "eye"
  | "scale"
  | "link"
  | "search"
  | "chevron-down"
  | "target"
  | "list"
  | "lightbulb"
  | "alert"
  | "wave"
  | "sun"
  | "moon";

/** SVG path data for each icon, drawn on a 24x24 grid. */
const PATHS: Record<IconName, React.ReactNode> = {
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4" />
    </>
  ),
  moon: <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />,
  users: (
    <>
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 19c.5-3.2 2.6-5 5.5-5s5 1.8 5.5 5" />
      <circle cx="17" cy="9" r="2.3" />
      <path d="M16 14.2c2.6-.2 4.2 1.3 4.7 4" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8.5" r="3.5" />
      <path d="M5 20c.7-3.7 3.4-5.5 7-5.5s6.3 1.8 7 5.5" />
    </>
  ),
  "user-plus": (
    <>
      <circle cx="10" cy="8.5" r="3.5" />
      <path d="M3.5 20c.6-3.7 3.2-5.5 6.5-5.5 1.5 0 2.8.3 3.8 1M18 14v6M15 17h6" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  bot: (
    <>
      <rect x="5" y="8" width="14" height="11" rx="3" />
      <path d="M12 4v4M9.5 13h.01M14.5 13h.01M9.5 16.2h5" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  history: (
    <>
      <path d="M4 12a8 8 0 1 0 2.4-5.7" />
      <path d="M4 4v4.5h4.5M12 8v4.2l2.8 1.8" />
    </>
  ),
  bell: (
    <>
      <path d="M6 16.5V11a6 6 0 1 1 12 0v5.5l1.5 1.5h-15z" />
      <path d="M10 20.5a2.2 2.2 0 0 0 4 0" />
    </>
  ),
  grid: (
    <>
      <rect x="4" y="4" width="7" height="7" rx="1.5" />
      <rect x="13" y="4" width="7" height="7" rx="1.5" />
      <rect x="4" y="13" width="7" height="7" rx="1.5" />
      <rect x="13" y="13" width="7" height="7" rx="1.5" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3.5" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3" />
    </>
  ),
  "mic-off": (
    <>
      <path d="M4 4l16 16" />
      <path d="M9 9v2.5a3 3 0 0 0 4.6 2.5M15 10.5V6.5a3 3 0 0 0-5.7-1.3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 10.6 5M18.5 11.5c0 .8-.1 1.5-.4 2.2M12 18v3" />
    </>
  ),
  video: (
    <>
      <rect x="3.5" y="6.5" width="12" height="11" rx="2.5" />
      <path d="M15.5 10.5l5-3v9l-5-3" />
    </>
  ),
  "video-off": (
    <>
      <path d="M4 4l16 16" />
      <path d="M9.5 6.5h3.5a2.5 2.5 0 0 1 2.5 2.5v3M15.5 14.5V15a2.5 2.5 0 0 1-2.5 2.5H6A2.5 2.5 0 0 1 3.5 15V9a2.5 2.5 0 0 1 1-2M15.5 10.5l5-3v9l-3-1.8" />
    </>
  ),
  volume: (
    <>
      <path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z" />
      <path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11" />
    </>
  ),
  "volume-off": (
    <>
      <path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z" />
      <path d="M16 9.5l5 5M21 9.5l-5 5" />
    </>
  ),
  chat: <path d="M5 5.5h14a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5h-7l-4.5 3.5V17.5H5A1.5 1.5 0 0 1 3.5 16V7A1.5 1.5 0 0 1 5 5.5z" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3.5v2.2M12 18.3v2.2M3.5 12h2.2M18.3 12h2.2M6 6l1.6 1.6M16.4 16.4L18 18M6 18l1.6-1.6M16.4 7.6L18 6" />
    </>
  ),
  "phone-off": (
    <>
      <path d="M4 4l16 16" />
      <path d="M3.5 13.5c1.6-3.8 5.2-5.5 8.5-5.5s6.900 1.700 8.500 5.500l-3 1.500-1.200-2.400c-1.300-.4-2.600-.6-4.300-.6s-3 .2-4.300.6L6.500 15z" />
    </>
  ),
  copy: (
    <>
      <rect x="8.5" y="8.5" width="11" height="11" rx="2" />
      <path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2" />
    </>
  ),
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  "check-circle": (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8.2 12.4l2.7 2.7 5-5.4" />
    </>
  ),
  trophy: (
    <>
      <path d="M8 4.5h8v5a4 4 0 0 1-8 0zM8 6.5H5a3 3 0 0 0 3 4M16 6.5h3a3 3 0 0 1-3 4" />
      <path d="M12 13.5V17M8.5 20h7M10 17h4" />
    </>
  ),
  download: (
    <>
      <path d="M12 4v10M7.5 10.5L12 15l4.5-4.5" />
      <path d="M5 19h14" />
    </>
  ),
  "arrow-right": <path d="M5 12h14M13 6l6 6-6 6" />,
  "arrow-left": <path d="M19 12H5M11 6l-6 6 6 6" />,
  shuffle: (
    <>
      <path d="M4 7h3.5c4 0 5 10 9 10H20M4 17h3.5c1.600 0 2.700-1.100 3.600-2.600M20 7h-3.500c-1.600 0-2.700 1.100-3.600 2.600" />
      <path d="M17.500 4.500L20 7l-2.500 2.500M17.500 14.500L20 17l-2.500 2.500" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="10.5" width="14" height="9.5" rx="2.5" />
      <path d="M8 10.500V8a4 4 0 0 1 8 0v2.500" />
    </>
  ),
  calendar: (
    <>
      <rect x="4" y="5.5" width="16" height="14.5" rx="2.500" />
      <path d="M4 10h16M8.500 3.500v4M15.500 3.500v4" />
    </>
  ),
  chart: (
    <>
      <path d="M4 20V4M4 20h16" />
      <path d="M8 16v-4M12.500 16V8M17 16v-6" />
    </>
  ),
  eye: (
    <>
      <path d="M2.500 12S6 5.500 12 5.500 21.500 12 21.500 12 18 18.500 12 18.500 2.500 12 2.500 12z" />
      <circle cx="12" cy="12" r="2.800" />
    </>
  ),
  scale: (
    <>
      <path d="M12 4v16M7 20h10M5 7.500h14" />
      <path d="M5 7.500L2.500 13.500a3 3 0 0 0 5 0zM19 7.500l-2.500 6a3 3 0 0 0 5 0z" />
    </>
  ),
  link: (
    <>
      <path d="M10 14a4 4 0 0 0 5.700 0l3-3a4 4 0 0 0-5.700-5.700l-1 1" />
      <path d="M14 10a4 4 0 0 0-5.700 0l-3 3a4 4 0 0 0 5.700 5.700l1-1" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6.500" />
      <path d="M16 16l4.500 4.500" />
    </>
  ),
  "chevron-down": <path d="M6 9.500l6 6 6-6" />,
  target: (
    <>
      <circle cx="12" cy="12" r="8.500" />
      <circle cx="12" cy="12" r="4.500" />
      <circle cx="12" cy="12" r="0.800" />
    </>
  ),
  list: <path d="M9 6.500h11M9 12h11M9 17.500h11M4.500 6.500h.01M4.500 12h.01M4.500 17.500h.01" />,
  lightbulb: (
    <>
      <path d="M9 17.500h6M10 20.500h4" />
      <path d="M12 3.500a6 6 0 0 0-3.500 10.800c.6.500 1 1.200 1 2V17.500h5v-1.200c0-.8.400-1.500 1-2A6 6 0 0 0 12 3.500z" />
    </>
  ),
  alert: (
    <>
      <path d="M12 4.500l8.500 15h-17z" />
      <path d="M12 10v4.500M12 17.500h.01" />
    </>
  ),
  wave: (
    <>
      <path d="M4 12h2M8 8v8M12 5v14M16 8v8M20 12h-2" />
    </>
  ),
};

/** Render a named line icon at the given size; extra props pass through to the <svg>. */
export function Icon({
  name,
  size = 20,
  ...rest
}: { name: IconName; size?: number } & Omit<SVGProps<SVGSVGElement>, "name">) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      focusable="false"
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}

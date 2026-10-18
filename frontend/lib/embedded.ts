/**
 * Is GD running inside the AI Interviewer platform's shell (M14 Day 4)?
 *
 * There it is one screen among the platform's, so it drops its own brand,
 * account menu and sign-out: the shell already has those, and two of each in
 * one window is two products. Set by /platform for the life of the frame's
 * tab; a frame is also enough on its own.
 */

const KEY = "gd.embedded";

export function markEmbedded(): void {
  try {
    sessionStorage.setItem(KEY, "1");
  } catch {
    /* private mode: being framed still says so */
  }
}

export function isEmbedded(): boolean {
  if (typeof window === "undefined") return false;
  try {
    if (sessionStorage.getItem(KEY) === "1") return true;
  } catch {
    /* fall through to the frame check */
  }
  try {
    return window.self !== window.top;
  } catch {
    return true; // a cross-origin parent refusing the comparison is a parent
  }
}

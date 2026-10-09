"use client";

/**
 * Plays the spoken lines of simulated (AI) participants.
 *
 * The server sends each line as base64 mp3 in an AGENT_AUDIO frame. Every line
 * gets its own audio element, so when one agent cuts in on another the two voices
 * genuinely overlap, as they do in a real discussion. The audio never goes into
 * React state: it is large and has no business in the render path.
 */

import { useSyncExternalStore } from "react";

const MUTE_KEY = "gd-arena-mute-ai-voices";

let muted = readMuted();
const listeners = new Set<() => void>();

function readMuted(): boolean {
  try {
    return window.localStorage.getItem(MUTE_KEY) === "1";
  } catch {
    return false;
  }
}

export function setAgentVoicesMuted(next: boolean): void {
  muted = next;
  try {
    window.localStorage.setItem(MUTE_KEY, next ? "1" : "0");
  } catch {
    // Storage can be unavailable (private window); the choice just won't persist.
  }
  listeners.forEach((listener) => listener());
}

export function useAgentVoicesMuted(): [boolean, (next: boolean) => void] {
  const value = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => muted,
    () => false,
  );
  return [value, setAgentVoicesMuted];
}

export function playAgentAudio(audioB64: string): void {
  if (muted || typeof window === "undefined") return;
  try {
    const binary = window.atob(audioB64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" }));
    const audio = new Audio(url);
    const release = () => URL.revokeObjectURL(url);
    audio.addEventListener("ended", release, { once: true });
    audio.addEventListener("error", release, { once: true });
    // Browsers block audio before the first click on the page; joining a room
    // involves one, but if it is still refused the transcript carries on silently.
    void audio.play().catch(release);
  } catch {
    // A malformed frame must not break the room.
  }
}

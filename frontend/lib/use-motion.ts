"use client";

import { useEffect, useState } from "react";  // state and effects for the animation hooks

/** True if the user prefers reduced motion (so animations are skipped). */
function reducedMotion(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/**
 * Counts from 0 up to `target`, easing out. With reduced motion it shows the
 * target at once. Browsers pause animation frames in a background tab, so a timer
 * also sets the final value: the number is right even if the tab was never visible.
 */
export function useCountUp(target: number, durationMs = 900): number {
  const [value, setValue] = useState(0);
  useEffect(() => {
    if (reducedMotion() || durationMs <= 0) {
      setValue(target);
      return;
    }
    let frame = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3);
      setValue(target * eased);
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    const settle = setTimeout(() => setValue(target), durationMs + 60);
    return () => {
      cancelAnimationFrame(frame);
      clearTimeout(settle);
    };
  }, [target, durationMs]);
  return value;
}

/**
 * Returns 0 on the first paint and `target` a moment later, so a CSS transition on
 * width or an @property has something to animate from. A timer, not an animation
 * frame, so it still lands in a background tab.
 */
export function useAfterMount(target: number): number {
  const [value, setValue] = useState(0);
  useEffect(() => {
    const id = setTimeout(() => setValue(target), 40);
    return () => clearTimeout(id);
  }, [target]);
  return value;
}

"use client";

import { useCallback, useEffect, useState } from "react";  // state and effects

import { ApiError } from "@/lib/api";  // error type

/** 1234 -> "1.2 s", 80 -> "80 ms". */
export const ms = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
/** Dollars with enough digits for tiny per-call costs. */
export const usd = (v: number) => (v === 0 ? "$0" : v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(3)}`);
/** 12345 -> "12.3k". */
export const num = (v: number) => (v >= 10_000 ? `${(v / 1000).toFixed(1)}k` : String(v));
/** Local time of day for an ISO timestamp. */
export const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour12: false });

/** Load data with the dashboard's shared loading/error handling; reloads when `deps` change. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  useEffect(() => {
    let live = true;
    run()
      .then((d) => live && (setData(d), setError(null)))
      .catch((e) => live && setError(e instanceof ApiError ? e.message : "could not load"));
    return () => {
      live = false;
    };
  }, [run]);
  return { data, error };
}

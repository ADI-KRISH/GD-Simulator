/** Mono 16-bit WAV from captured microphone frames, resampled for speech recognition. */
export function encodeWav(frames: Float32Array[], inRate: number, outRate = 16000): ArrayBuffer {
  const total = frames.reduce((n, f) => n + f.length, 0);
  const input = new Float32Array(total);
  let offset = 0;
  for (const frame of frames) {
    input.set(frame, offset);
    offset += frame.length;
  }

  // Linear-interpolation resample; speech recognisers are happy with 16 kHz.
  const ratio = inRate / outRate;
  const length = Math.floor(total / ratio);
  const pcm = new Int16Array(length);
  for (let i = 0; i < length; i++) {
    const pos = i * ratio;
    const lo = Math.floor(pos);
    const hi = Math.min(lo + 1, total - 1);
    const sample = input[lo] + (input[hi] - input[lo]) * (pos - lo);
    pcm[i] = Math.max(-1, Math.min(1, sample)) * 0x7fff;
  }

  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const text = (at: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + pcm.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, outRate, true);
  view.setUint32(28, outRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, pcm.length * 2, true);
  new Int16Array(buffer, 44).set(pcm);
  return buffer;
}

/**
 * Links and pointer probing for the PTY-backed dashboard chat (`/chat`).
 *
 * These two helpers outlived the structured-chat experiment they were first
 * written for: `/chat/structured` was disabled and its page removed, but a
 * deep link into the PTY view of a known session and the coarse-pointer probe
 * that phone-specific chat chrome keys off are still live.
 */

/** Deep link to the PTY chat for one session, with the diagnostic `pty=1` flag. */
export function ptyChatHref(sessionId: string, profile = ""): string {
  const params = new URLSearchParams();
  params.set("resume", sessionId);
  if (profile) params.set("profile", profile);
  params.set("pty", "1");
  return `/chat?${params.toString()}`;
}

/** Read the pointer traits that distinguish a phone from a laptop digitizer. */
export function readPhonePointer(): { maxTouchPoints: number; pointerCoarse: boolean } {
  if (typeof navigator === "undefined" || typeof window === "undefined") {
    return { maxTouchPoints: 0, pointerCoarse: false };
  }
  const coarse = typeof window.matchMedia === "function"
    && window.matchMedia("(pointer: coarse)").matches;
  return { maxTouchPoints: navigator.maxTouchPoints ?? 0, pointerCoarse: coarse };
}

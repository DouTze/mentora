import type { KeyboardEvent } from "react";

/** The key press belongs to an input method (選字 with Enter in a Chinese IME), so it must not send or submit.
 * WebKit (Safari, the desktop app) ends the composition before the Enter's keydown, so `isComposing` is already false
 * there; that keydown still carries keyCode 229, the code every engine gives a key the IME has taken. */
export function composing(e: KeyboardEvent): boolean {
  return e.nativeEvent.isComposing || e.keyCode === 229;
}

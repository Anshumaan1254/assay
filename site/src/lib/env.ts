/* What this browser will actually let us do.
 *
 * Both checks decide whether the WebGL bundle is fetched at all, so they
 * are cheap, synchronous, and run before the dynamic import.
 */

/** Does the visitor want less motion? Read once at mount rather than
 *  subscribed to: swapping a scrolling reader between a canvas and a static
 *  storyboard mid-scroll is itself a motion event. */
export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Can we get a WebGL context at all?
 *
 * Probed on a throwaway canvas, because the failure modes worth catching
 * here are the ones where `WebGLRenderingContext` exists on `window` and
 * context creation still fails: blocklisted drivers, a headless or
 * software-rendering environment, or too many live contexts already. The
 * probe canvas is explicitly lost afterwards so it does not count against
 * that last limit itself.
 */
export function hasWebGL(): boolean {
  if (typeof document === "undefined") return false;
  try {
    const canvas = document.createElement("canvas");
    const gl = (canvas.getContext("webgl2") ??
      canvas.getContext("webgl")) as WebGLRenderingContext | null;
    if (!gl) return false;
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    return true;
  } catch {
    return false;
  }
}

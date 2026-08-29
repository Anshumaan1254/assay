/* One Lenis for the whole page.
 *
 * Both pasted components create their own Lenis instance in their own
 * effect. Two instances both hijacking wheel input and both driving
 * ScrollTrigger.update is not a smoother page -- it is double-applied
 * easing and a scroll position two objects disagree about. They now share
 * this refcounted singleton instead, which keeps each component
 * self-contained (it still just asks for smooth scroll and releases it on
 * unmount) while guaranteeing there is only ever one.
 *
 * Also the single place the GSAP ticker is wired, so the ticker callback is
 * added and removed exactly once.
 */

import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import Lenis from "@studio-freight/lenis";

let instance: Lenis | null = null;
let ticker: ((time: number) => void) | null = null;
let holders = 0;

/** Start (or join) smooth scrolling. Returns the release function. */
export function acquireSmoothScroll(): () => void {
  holders += 1;

  if (holders === 1) {
    // A reader who asked for less motion gets the browser's own scrolling,
    // not a eased reinterpretation of it.
    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    if (!reduced) {
      instance = new Lenis({ duration: 1.05, smoothWheel: true });
      instance.on("scroll", ScrollTrigger.update);
      ticker = (time: number) => instance?.raf(time * 1000);
      gsap.ticker.add(ticker);
      gsap.ticker.lagSmoothing(0);
    }
  }

  let released = false;
  return () => {
    if (released) return;
    released = true;
    holders -= 1;
    if (holders <= 0) {
      holders = 0;
      if (ticker) gsap.ticker.remove(ticker);
      instance?.destroy();
      instance = null;
      ticker = null;
    }
  };
}

export function getSmoothScroll(): Lenis | null {
  return instance;
}

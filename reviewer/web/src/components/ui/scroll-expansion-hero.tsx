"use client";

/*
 * Adapted from the scroll-expansion hero component.
 *
 * Four things had to change to run here, none cosmetic:
 *
 * 1. `next/image` is gone. That is a Next.js component and this app is
 *    Vite + React; the import does not resolve at all. Plain <img> instead.
 *
 * 2. `bgImageSrc` is optional. The upstream demo points at Unsplash and
 *    Pexels stock URLs. A settlement audit has no stock photography, and
 *    fetching decorative images from third-party CDNs on a page showing a
 *    merchant's money is not a trade worth making. Absent a background it
 *    renders a gradient built from the page's own palette.
 *
 * 3. It yields scroll instead of owning it. Upstream hijacks the wheel
 *    (`preventDefault` on every event) and pins the window with
 *    `scrollTo(0, 0)` until fully expanded. This page also runs a GSAP
 *    ScrollTrigger pinned/scrubbed section, and two systems driving scroll
 *    fight each other. The hijack is now scoped to "the gate has not been
 *    passed AND the hero is still at the top of the document", and
 *    `onExpanded` fires once so the page can call ScrollTrigger.refresh()
 *    and recompute positions against the now-taller document.
 *
 * 4. It can be skipped, and it self-disables under prefers-reduced-motion.
 *    Forcing a reviewer to wheel through an animation to reach the number
 *    they came for is hostile; a scroll gate must never be the only way in.
 *
 * Native event types are used throughout rather than upstream's React
 * synthetic types plus `as unknown as EventListener` casts, which defeated
 * type checking on every listener.
 */

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";

interface ScrollExpandMediaProps {
  mediaType?: "video" | "image" | "node";
  /** Required for "video" and "image"; ignored for "node". */
  mediaSrc?: string;
  /** A live component to expand instead of a file. The expanding box is
   * sized by scroll, so whatever goes here must fill its container and
   * cope with being resized continuously. */
  mediaNode?: ReactNode;
  posterSrc?: string;
  bgImageSrc?: string;
  title?: string;
  date?: string;
  scrollToExpand?: string;
  textBlend?: boolean;
  onExpanded?: () => void;
  children?: ReactNode;
}

const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const ScrollExpandMedia = ({
  mediaType = "video",
  mediaSrc,
  mediaNode,
  posterSrc,
  bgImageSrc,
  title,
  date,
  scrollToExpand,
  textBlend,
  onExpanded,
  children,
}: ScrollExpandMediaProps) => {
  const reduced = prefersReducedMotion();
  const [scrollProgress, setScrollProgress] = useState<number>(reduced ? 1 : 0);
  const [showContent, setShowContent] = useState<boolean>(reduced);
  const [mediaFullyExpanded, setMediaFullyExpanded] = useState<boolean>(reduced);
  const [touchStartY, setTouchStartY] = useState<number>(0);
  const [isMobileState, setIsMobileState] = useState<boolean>(false);

  const sectionRef = useRef<HTMLDivElement | null>(null);
  const announced = useRef(false);

  const expand = useCallback(() => {
    setScrollProgress(1);
    setMediaFullyExpanded(true);
    setShowContent(true);
  }, []);

  // Fires exactly once, after the gate opens, so the host page can
  // recompute any scroll-driven layout against the taller document.
  useEffect(() => {
    if (mediaFullyExpanded && !announced.current) {
      announced.current = true;
      onExpanded?.();
    }
  }, [mediaFullyExpanded, onExpanded]);

  useEffect(() => {
    if (reduced) return;

    const gateIsActive = () => !mediaFullyExpanded && window.scrollY <= 5;

    const advance = (delta: number) => {
      const next = Math.min(Math.max(scrollProgress + delta, 0), 1);
      setScrollProgress(next);
      if (next >= 1) {
        setMediaFullyExpanded(true);
        setShowContent(true);
      } else if (next < 0.75) {
        setShowContent(false);
      }
    };

    const handleWheel = (e: globalThis.WheelEvent) => {
      if (mediaFullyExpanded && e.deltaY < 0 && window.scrollY <= 5) {
        setMediaFullyExpanded(false);
        setScrollProgress(0.99);
        e.preventDefault();
      } else if (gateIsActive()) {
        e.preventDefault();
        advance(e.deltaY * 0.0009);
      }
    };

    const handleTouchStart = (e: globalThis.TouchEvent) => setTouchStartY(e.touches[0].clientY);

    const handleTouchMove = (e: globalThis.TouchEvent) => {
      if (!touchStartY) return;
      const touchY = e.touches[0].clientY;
      const deltaY = touchStartY - touchY;

      if (mediaFullyExpanded && deltaY < -20 && window.scrollY <= 5) {
        setMediaFullyExpanded(false);
        e.preventDefault();
      } else if (gateIsActive()) {
        e.preventDefault();
        advance(deltaY * (deltaY < 0 ? 0.008 : 0.005));
        setTouchStartY(touchY);
      }
    };

    const handleTouchEnd = () => setTouchStartY(0);

    // Only clamps the window while the gate is genuinely active. Upstream
    // clamped unconditionally, which fought ScrollTrigger and yanked a
    // mid-page reload back to the top.
    const handleScroll = () => {
      if (!mediaFullyExpanded && window.scrollY > 5) setMediaFullyExpanded(true);
    };

    window.addEventListener("wheel", handleWheel, { passive: false });
    window.addEventListener("scroll", handleScroll);
    window.addEventListener("touchstart", handleTouchStart, { passive: false });
    window.addEventListener("touchmove", handleTouchMove, { passive: false });
    window.addEventListener("touchend", handleTouchEnd);

    return () => {
      window.removeEventListener("wheel", handleWheel);
      window.removeEventListener("scroll", handleScroll);
      window.removeEventListener("touchstart", handleTouchStart);
      window.removeEventListener("touchmove", handleTouchMove);
      window.removeEventListener("touchend", handleTouchEnd);
    };
  }, [scrollProgress, mediaFullyExpanded, touchStartY, reduced]);

  useEffect(() => {
    const checkIfMobile = () => setIsMobileState(window.innerWidth < 768);
    checkIfMobile();
    window.addEventListener("resize", checkIfMobile);
    return () => window.removeEventListener("resize", checkIfMobile);
  }, []);

  const mediaWidth = 300 + scrollProgress * (isMobileState ? 650 : 1250);
  const mediaHeight = 400 + scrollProgress * (isMobileState ? 200 : 400);
  const textTranslateX = scrollProgress * (isMobileState ? 180 : 150);

  const firstWord = title ? title.split(" ")[0] : "";
  const restOfTitle = title ? title.split(" ").slice(1).join(" ") : "";

  return (
    <div ref={sectionRef} className="transition-colors duration-700 ease-in-out overflow-x-hidden">
      <section className="relative flex flex-col items-center justify-start min-h-[100dvh]">
        <div className="relative w-full flex flex-col items-center min-h-[100dvh]">
          <motion.div
            className="absolute inset-0 z-0 h-full"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 - scrollProgress }}
            transition={{ duration: 0.1 }}
          >
            {bgImageSrc ? (
              <img
                src={bgImageSrc}
                alt=""
                className="w-screen h-screen"
                style={{ objectFit: "cover", objectPosition: "center" }}
              />
            ) : (
              <div
                className="w-screen h-screen"
                style={{
                  background:
                    "radial-gradient(60rem 40rem at 20% 0%, rgba(232,180,74,0.10), transparent 60%)," +
                    "radial-gradient(50rem 34rem at 85% 10%, rgba(107,169,255,0.08), transparent 60%)," +
                    "var(--bg)",
                }}
              />
            )}
            <div className="absolute inset-0 bg-black/10" />
          </motion.div>

          {!mediaFullyExpanded && !reduced && (
            <button
              onClick={expand}
              className="fixed top-20 right-6 z-30 rounded-md border px-3 py-1.5 text-xs uppercase tracking-widest"
              style={{
                borderColor: "var(--line-bright)",
                color: "var(--ink-dim)",
                background: "var(--bg-panel)",
              }}
            >
              Skip to report
            </button>
          )}

          <div className="container mx-auto flex flex-col items-center justify-start relative z-10">
            <div className="flex flex-col items-center justify-center w-full h-[100dvh] relative">
              <div
                className="absolute z-0 top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 transition-none rounded-2xl"
                style={{
                  width: `${mediaWidth}px`,
                  height: `${mediaHeight}px`,
                  maxWidth: "95vw",
                  maxHeight: "85vh",
                  boxShadow: "0px 0px 50px rgba(0, 0, 0, 0.3)",
                }}
              >
                {mediaType === "node" ? (
                  <div className="relative h-full w-full overflow-hidden rounded-xl">
                    {mediaNode}
                    <motion.div
                      className="absolute inset-0 bg-black/60 rounded-xl pointer-events-none"
                      initial={{ opacity: 0.75 }}
                      animate={{ opacity: 0.75 - scrollProgress * 0.75 }}
                      transition={{ duration: 0.2 }}
                    />
                  </div>
                ) : mediaType === "video" ? (
                  <div className="relative w-full h-full pointer-events-none">
                    <video
                      src={mediaSrc}
                      poster={posterSrc}
                      autoPlay
                      muted
                      loop
                      playsInline
                      preload="auto"
                      className="w-full h-full object-cover rounded-xl"
                      controls={false}
                      disablePictureInPicture
                      disableRemotePlayback
                    />
                    <motion.div
                      className="absolute inset-0 bg-black/30 rounded-xl"
                      initial={{ opacity: 0.7 }}
                      animate={{ opacity: 0.5 - scrollProgress * 0.3 }}
                      transition={{ duration: 0.2 }}
                    />
                  </div>
                ) : (
                  <div className="relative w-full h-full">
                    <img
                      src={mediaSrc ?? ""}
                      alt={title || "Media content"}
                      className="w-full h-full object-contain rounded-xl"
                      style={{ background: "var(--bg-raised)" }}
                    />
                    <motion.div
                      className="absolute inset-0 bg-black/50 rounded-xl pointer-events-none"
                      initial={{ opacity: 0.7 }}
                      animate={{ opacity: 0.7 - scrollProgress * 0.6 }}
                      transition={{ duration: 0.2 }}
                    />
                  </div>
                )}

                <div className="flex flex-col items-center text-center relative z-10 mt-4 transition-none">
                  {date && (
                    <p
                      className="text-2xl"
                      style={{
                        color: "var(--gold)",
                        transform: `translateX(-${textTranslateX}vw)`,
                      }}
                    >
                      {date}
                    </p>
                  )}
                  {scrollToExpand && (
                    <p
                      className="font-medium text-center"
                      style={{
                        color: "var(--ink-dim)",
                        transform: `translateX(${textTranslateX}vw)`,
                      }}
                    >
                      {scrollToExpand}
                    </p>
                  )}
                </div>
              </div>

              <div
                className={`flex items-center justify-center text-center gap-4 w-full relative z-10 transition-none flex-col ${
                  textBlend ? "mix-blend-difference" : "mix-blend-normal"
                }`}
              >
                <motion.h2
                  className="text-4xl md:text-5xl lg:text-6xl font-bold transition-none"
                  style={{ color: "var(--ink)", transform: `translateX(-${textTranslateX}vw)` }}
                >
                  {firstWord}
                </motion.h2>
                <motion.h2
                  className="text-4xl md:text-5xl lg:text-6xl font-bold text-center transition-none"
                  style={{ color: "var(--ink)", transform: `translateX(${textTranslateX}vw)` }}
                >
                  {restOfTitle}
                </motion.h2>
              </div>
            </div>

            <motion.section
              className="flex flex-col w-full px-8 py-10 md:px-16 lg:py-20"
              initial={{ opacity: 0 }}
              animate={{ opacity: showContent ? 1 : 0 }}
              transition={{ duration: 0.7 }}
            >
              {children}
            </motion.section>
          </div>
        </div>
      </section>
    </div>
  );
};

export default ScrollExpandMedia;

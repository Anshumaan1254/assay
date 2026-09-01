"use client"

import { useEffect, useRef, useState } from "react"

// ─────────────────────────────────────────────────────────────
// THE MONEY ARRIVES — locked scroll-scrub video hero
// The page cannot move while this is active — body is pinned
// with position:fixed (the same bulletproof technique modal
// libraries use; plain overflow:hidden alone isn't reliable
// across browsers). Wheel/touch input is captured and used
// purely to drive video.currentTime, forward and backward. Once
// the video reaches the end and the user keeps pushing forward,
// the page unlocks and continues normally — and re-locks if they
// scroll back up into it. No dependencies, system fonts only.
// ─────────────────────────────────────────────────────────────

export interface MetroHeroProps {
  videoSrc?: string
  title?: string
  scrollHint?: string
  tagline?: string
  signature?: { name: string; url: string } | false
  /** Total input distance (px) needed to scrub the full video. Tune to taste. */
  scrubDistance?: number
  className?: string
  style?: React.CSSProperties
}

// Self-hosted. From raw.githubusercontent.com this was 9.3 MB over 26s with
// a five-minute cache and an octet-stream content-type; from site/public it
// comes off Vercel's edge. Relative, so Vite's `base` still applies.
const DEFAULT_VIDEO = "video/hero.mp4"
const DEFAULT_SIGNATURE = { name: "guglielmogiannattasio.exe", url: "https://www.guglielmogiannattasio.it" }
const SANS = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

// Reviewer palette — this page and reviewer/web share one ground and one accent.
const COL_BG = "#08090b"
const COL_TEXT = "#e8ebf0"
const COL_ACCENT = "#e8b44a"

function clamp(v: number, min: number, max: number) {
  return Math.min(max, Math.max(min, v))
}

export default function MetroHero({
  videoSrc = DEFAULT_VIDEO,
  title = "THE MONEY ARRIVES",
  scrollHint = "SCROLL",
  tagline = "You were told what was deducted. You were never shown.",
  signature = DEFAULT_SIGNATURE,
  scrubDistance = 3200,
  className,
  style,
}: MetroHeroProps) {
  const sectionRef = useRef<HTMLDivElement>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const titleRef = useRef<HTMLDivElement>(null)
  const hintRef = useRef<HTMLDivElement>(null)
  const taglineRef = useRef<HTMLDivElement>(null)
  const progressBarRef = useRef<HTMLDivElement>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const video = videoRef.current
    const section = sectionRef.current
    if (!video || !section) return

    const reduceMotion =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches

    let duration = 0
    let rafId = 0
    let targetProgress = 0
    let currentProgress = 0
    let hasStartedScrolling = false
    let isSeeking = false
    let pendingTime: number | null = null
    let locked = false
    let lockedScrollY = 0
    let touchStartY = 0

    const onLoadedData = () => {
      duration = video.duration || 0
      setReady(true)
      if (reduceMotion) {
        video.currentTime = duration * 0.92
        return
      }
      // The lock is taken HERE, not at mount. Locking before the first frame
      // exists gave the visitor a black screen they could not scroll past for
      // as long as the download took -- which on the old host was 26 seconds,
      // and reads as a broken site rather than as a slow one.
      settleLock(true)
    }
    video.addEventListener("loadeddata", onLoadedData)

    // If the video cannot load at all, this section must degrade to an
    // ordinary scrollable panel. A hero that fails is a hero you scroll past,
    // never a locked black box.
    const onError = () => {
      setFailed(true)
      settleLock(false)
    }
    video.addEventListener("error", onError)

    const onSeeked = () => {
      isSeeking = false
      if (pendingTime !== null) {
        const t = pendingTime
        pendingTime = null
        isSeeking = true
        video.currentTime = t
      }
    }
    video.addEventListener("seeked", onSeeked)

    // `seekTo` is a hoisted declaration, so TypeScript cannot prove it runs
    // after the null guard above. Capturing the narrowed element keeps the
    // guard meaningful instead of asserting it away.
    const media = video

    function seekTo(t: number) {
      if (isSeeking) {
        pendingTime = t
        return
      }
      isSeeking = true
      media.currentTime = t
    }

    function engageLock() {
      if (locked || typeof document === "undefined") return
      locked = true
      lockedScrollY = window.scrollY
      const b = document.body.style
      b.position = "fixed"
      b.top = `-${lockedScrollY}px`
      b.left = "0"
      b.right = "0"
      b.width = "100%"
    }

    function releaseLock() {
      if (!locked || typeof document === "undefined") return
      locked = false
      const y = lockedScrollY
      const b = document.body.style
      b.position = ""
      b.top = ""
      b.left = ""
      b.right = ""
      b.width = ""
      window.scrollTo(0, y)
    }

    // Decided once, by whichever of loadeddata / error / the watchdog arrives
    // first. `settled` makes it idempotent so a late event cannot re-lock a
    // page the reader has already scrolled on from.
    let settled = false
    function settleLock(canPlay: boolean) {
      if (settled) return
      settled = true
      window.clearTimeout(watchdog)
      // A reader who cannot see the animation must never be trapped by it:
      // reduced motion skips the lock entirely and the page scrolls normally
      // past a hero already parked on its final frame.
      if (canPlay && !reduceMotion) engageLock()
    }

    // Last resort. If neither event fires -- a stalled connection, a codec the
    // browser accepts but never decodes -- the page must still be usable, so
    // give up on locking rather than waiting forever.
    const watchdog = window.setTimeout(() => settleLock(false), 6000)

    function addDelta(deltaY: number) {
      targetProgress = clamp(targetProgress + deltaY / scrubDistance, 0, 1)
      if (targetProgress > 0.001) hasStartedScrolling = true
    }

    const AT_END = 1 - 1e-4

    const onWheel = (e: WheelEvent) => {
      if (!locked) {
        // Re-lock only when the reader has scrolled all the way back to the
        // top and is still pushing upward — otherwise the hero would grab
        // the page back the moment they glance up mid-document.
        if (window.scrollY <= 0 && e.deltaY < 0) {
          targetProgress = 1
          currentProgress = 1
          engageLock()
          e.preventDefault()
        }
        return
      }
      // The release valve the header comment promises: at the end of the
      // video, forward input stops driving the scrub and hands the page back.
      if (targetProgress >= AT_END && e.deltaY > 0) {
        releaseLock()
        return
      }
      addDelta(e.deltaY)
      e.preventDefault()
    }

    const onTouchStart = (e: TouchEvent) => {
      touchStartY = e.touches[0]?.clientY ?? 0
    }
    const onTouchMove = (e: TouchEvent) => {
      const y = e.touches[0]?.clientY ?? touchStartY
      const deltaY = touchStartY - y
      touchStartY = y
      if (!locked) {
        if (window.scrollY <= 0 && deltaY < 0) {
          targetProgress = 1
          currentProgress = 1
          engageLock()
          e.preventDefault()
        }
        return
      }
      if (targetProgress >= AT_END && deltaY > 0) {
        releaseLock()
        return
      }
      addDelta(deltaY)
      e.preventDefault()
    }

    // A keyboard or screen-reader user gets the same escape hatch: any key
    // that means "move on" releases the hero rather than dead-ending them.
    const onKeyDown = (e: KeyboardEvent) => {
      if (!locked) return
      if (["ArrowDown", "PageDown", "End", " ", "Escape", "Tab"].includes(e.key)) {
        targetProgress = 1
        releaseLock()
      }
    }

    window.addEventListener("wheel", onWheel, { passive: false })
    window.addEventListener("touchstart", onTouchStart, { passive: true })
    window.addEventListener("touchmove", onTouchMove, { passive: false })
    window.addEventListener("keydown", onKeyDown)

    function frame() {
      currentProgress += (targetProgress - currentProgress) * 0.18

      if (duration > 0) {
        seekTo(currentProgress * duration)
      }

      if (videoRef.current) {
        const scale = 1 + currentProgress * 0.06
        videoRef.current.style.transform = `scale(${scale})`
      }
      if (titleRef.current) {
        const t = 1 - clamp(currentProgress / 0.35, 0, 1)
        titleRef.current.style.opacity = String(t)
        titleRef.current.style.transform = `translateY(${(1 - t) * -24}px) scale(${0.96 + t * 0.04})`
        titleRef.current.style.filter = `blur(${(1 - t) * 10}px)`
      }
      if (hintRef.current) {
        hintRef.current.style.opacity = hasStartedScrolling ? "0" : "1"
      }
      if (taglineRef.current) {
        // Mirrors the title's blur-focus treatment, timed as the payoff
        // once the reveal is nearly complete — not a background afterthought.
        const t = clamp((currentProgress - 0.82) / 0.18, 0, 1)
        taglineRef.current.style.opacity = String(t)
        taglineRef.current.style.transform = `translateY(${(1 - t) * 20}px) scale(${0.97 + t * 0.03})`
        taglineRef.current.style.filter = `blur(${(1 - t) * 8}px)`
      }
      if (progressBarRef.current) {
        progressBarRef.current.style.transform = `scaleX(${currentProgress})`
      }

      rafId = requestAnimationFrame(frame)
    }

    if (!reduceMotion) {
      rafId = requestAnimationFrame(frame)
    }

    return () => {
      window.clearTimeout(watchdog)
      video.removeEventListener("loadeddata", onLoadedData)
      video.removeEventListener("error", onError)
      video.removeEventListener("seeked", onSeeked)
      window.removeEventListener("wheel", onWheel)
      window.removeEventListener("touchstart", onTouchStart)
      window.removeEventListener("touchmove", onTouchMove)
      window.removeEventListener("keydown", onKeyDown)
      cancelAnimationFrame(rafId)
      releaseLock()
    }
  }, [scrubDistance])

  return (
    <div
      ref={sectionRef}
      className={className}
      style={{
        position: "relative",
        height: "100dvh",
        width: "100%",
        overflow: "hidden",
        background: COL_BG,
        ...style,
      }}
    >
      {/* The ground the video sits on. Before `ready` the <video> is still at
          opacity 0, and without this the hero is pure black -- which is what
          made a slow load look like a broken page. */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "radial-gradient(120% 90% at 50% 40%, #12151b 0%, #0a0c10 45%, #08090b 100%)",
        }}
      />

      <video
        ref={videoRef}
        src={videoSrc}
        muted
        playsInline
        preload="auto"
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          objectFit: "cover",
          opacity: ready ? 1 : 0,
          transformOrigin: "center center",
          willChange: "transform",
          transition: "opacity 0.6s ease",
        }}
      />

      <div
        style={{
          position: "absolute",
          inset: 0,
          background: "linear-gradient(180deg, rgba(8,9,11,0.45), rgba(8,9,11,0) 30%, rgba(8,9,11,0.2) 70%, rgba(8,9,11,0.7))",
          pointerEvents: "none",
        }}
      />

      <div
        ref={titleRef}
        style={{
          position: "absolute",
          inset: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "0 6%",
          textAlign: "center",
          pointerEvents: "none",
        }}
      >
        <span
          style={{
            fontFamily: SANS,
            fontWeight: 800,
            fontSize: "clamp(30px, 7vw, 96px)",
            lineHeight: 1,
            letterSpacing: "-0.03em",
            color: COL_TEXT,
            textShadow: "0 4px 30px rgba(0,0,0,0.5)",
            display: "inline-block",
            willChange: "transform, filter, opacity",
          }}
        >
          {title}
        </span>
      </div>

      {tagline && (
        <div
          ref={taglineRef}
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "0 8%",
            textAlign: "center",
            opacity: 0,
            pointerEvents: "none",
          }}
        >
          <span
            style={{
              fontFamily: SANS,
              fontWeight: 700,
              fontSize: "clamp(20px, 3.4vw, 40px)",
              lineHeight: 1.2,
              letterSpacing: "-0.02em",
              color: COL_TEXT,
              textShadow: "0 4px 24px rgba(0,0,0,0.5)",
            }}
          >
            {tagline}
          </span>
        </div>
      )}

      {!ready && (
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: "62%",
            transform: "translateX(-50%)",
            fontFamily: SANS,
            fontSize: "clamp(10px, 1.4vw, 12px)",
            fontWeight: 600,
            letterSpacing: "0.3em",
            color: "rgba(139,148,163,0.7)",
            pointerEvents: "none",
          }}
        >
          {failed ? "SCROLL ON" : "LOADING"}
        </div>
      )}

      <div
        ref={hintRef}
        style={{
          position: "absolute",
          left: "50%",
          bottom: "clamp(20px, 6vh, 48px)",
          transform: "translateX(-50%)",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 8,
          color: "rgba(232,235,240,0.75)",
          fontFamily: SANS,
          fontSize: "clamp(10px, 1.4vw, 12px)",
          fontWeight: 600,
          letterSpacing: "0.3em",
          transition: "opacity 0.4s ease",
          pointerEvents: "none",
        }}
      >
        <span>{scrollHint}</span>
        <svg width="14" height="18" viewBox="0 0 14 18" style={{ animation: "metro-hero-bounce 1.6s ease-in-out infinite" }}>
          <style>{`
            @keyframes metro-hero-bounce {
              0%, 100% { transform: translateY(0); opacity: 0.5; }
              50% { transform: translateY(5px); opacity: 1; }
            }
          `}</style>
          <path d="M7 1 L7 17 M2 12 L7 17 L12 12" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>

      {/* Thin progress line — fills as the video advances. */}
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 0,
          height: 2,
          background: "rgba(255,255,255,0.10)",
        }}
      >
        <div
          ref={progressBarRef}
          style={{
            height: "100%",
            width: "100%",
            background: `linear-gradient(90deg, rgba(232,180,74,0.35), ${COL_ACCENT})`,
            transform: "scaleX(0)",
            transformOrigin: "left center",
          }}
        />
      </div>

      {signature && (
        <span
          style={{
            position: "absolute",
            right: "clamp(12px, 2.5vw, 24px)",
            bottom: "clamp(10px, 2vw, 18px)",
            fontFamily: SANS,
            fontWeight: 500,
            fontSize: "clamp(11px, 1.4vw, 13px)",
            letterSpacing: "0.01em",
            color: "rgba(139,148,163,0.75)",
            zIndex: 2,
          }}
        >
          by{" "}
          <a
            href={signature.url}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: "rgba(139,148,163,0.75)",
              textDecoration: "none",
              transition: "color 0.2s ease",
            }}
            onMouseEnter={(e: React.MouseEvent<HTMLAnchorElement>) => {
              e.currentTarget.style.color = COL_TEXT
            }}
            onMouseLeave={(e: React.MouseEvent<HTMLAnchorElement>) => {
              e.currentTarget.style.color = "rgba(139,148,163,0.75)"
            }}
          >
            {signature.name}
          </a>
        </span>
      )}
    </div>
  )
}

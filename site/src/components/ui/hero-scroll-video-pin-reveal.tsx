'use client';

import React, { useEffect, useRef } from 'react';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { SplitText } from 'gsap/SplitText';

import { acquireSmoothScroll } from '@/lib/smooth-scroll';

gsap.registerPlugin(ScrollTrigger, SplitText);

// The reviewer's ground. This section stays opaque on purpose: the reveal is
// a clip-path circle widening out of nothing, and it only reads as a reveal
// against a solid backdrop.
const GROUND = '#08090b';

// ScrollTrigger creates the pin spacer itself and GSAP's types do not
// describe it, but it is a real element and it defaults to transparent --
// which shows the page ground through the pinned section at exactly the
// moment the reveal needs a solid backdrop. One helper, so the cast lives in
// a single place rather than at all six call sites.
type PinnedTrigger = ScrollTrigger & { spacer?: HTMLElement; pin?: HTMLElement };

function paintPinBackground(self: ScrollTrigger) {
  const pinned = self as PinnedTrigger;
  if (pinned.spacer) pinned.spacer.style.backgroundColor = GROUND;
  if (pinned.pin) pinned.pin.style.backgroundColor = GROUND;
}

/** A line that surfaces at one point in the pinned reveal and clears again.
 *  `at` is a fraction of the pin timeline; `position` is where on the frame
 *  it lands. They are spread across the frame deliberately, so the reveal
 *  moves the eye around the video rather than parking it in the middle. */
export interface VideoCaption {
  text: string;
  at: number;
  position: 'tl' | 'tr' | 'bl' | 'br' | 'c';
  accent?: boolean;
}

export interface TagItem {
  id?: string;
  text: string;
  background: string;
  color?: string;
}

const DEFAULT_CAPTIONS: VideoCaption[] = [
  { text: 'Open the credit.', at: 0.06, position: 'tl' },
  { text: 'Find the transactions inside it.', at: 0.28, position: 'br' },
  { text: 'Reprice every deduction.', at: 0.5, position: 'tr' },
  { text: 'Report what will not close.', at: 0.72, position: 'bl', accent: true },
];

export interface HeroScrollVideoRevealProps {
  topText?: React.ReactNode;
  headingText?: React.ReactNode;
  tags?: TagItem[];
  subText?: string;
  videoSrc?: string;
  bottomText?: React.ReactNode;
  captions?: VideoCaption[];
  badgeImgSrc?: string;
  className?: string;
}

const DEFAULT_TAGS: TagItem[] = [
  { text: 'Integer paise only', background: '#111419', color: '#e8ebf0' },
  { text: 'No model computes money', background: '#1c212a', color: '#e8ebf0' },
  { text: '₹0.00 falsely claimed', background: '#e8b44a', color: '#08090b' },
  { text: 'Byte-identical replay', background: '#2a313d', color: '#e8ebf0' },
];

export const HeroScrollVideoReveal: React.FC<HeroScrollVideoRevealProps> = ({
  topText = (
    <>
      Reconciliation checks that rows match.
      <br />
      It never checks the price.
    </>
  ),
  headingText = (
    <>
      Recompute every rupee. <br />
      Cite every one.
    </>
  ),
  tags = DEFAULT_TAGS,
  subText = '18 audited months. 1,400 findings. Zero invented.',
  videoSrc = 'https://res.cloudinary.com/dsuwzuaxp/video/upload/856381-hd_1920_1080_30fps_gsq11b.mp4',
  bottomText = (
    <>
      Three payments belonged to nothing.
      <br />
      It said so, in rupees.
    </>
  ),
  captions = DEFAULT_CAPTIONS,
  // No rotating "PLAY VIDEO" badge by default: nothing here is playable, so
  // the affordance was a lie. Pass a URL to bring it back.
  badgeImgSrc = '',
  className = '',
}) => {
  const benefitRef = useRef<HTMLDivElement>(null);
  const videoWrapperRef = useRef<HTMLDivElement>(null);
  const videoBoxRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const paraRef = useRef<HTMLParagraphElement>(null);
  const tagRefs = useRef<(HTMLDivElement | null)[]>([]);
  const tagRowRef = useRef<HTMLDivElement>(null);
  const captionRefs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    // Ensure video plays smoothly
    if (videoRef.current) {
      videoRef.current.defaultMuted = true;
      videoRef.current.muted = true;
      videoRef.current.play().catch(() => {});
    }

    // Smooth scroll, shared with every other section rather than a second
    // Lenis of its own -- see lib/smooth-scroll.ts.
    const releaseSmoothScroll = acquireSmoothScroll();

    // Word split kinetic reveal animation
    let split: any = null;
    let words: Element[] = [];

    try {
      split = new SplitText(paraRef.current, {
        type: 'words',
        wordsClass: 'reveal-word inline-block origin-left mr-[0.25em] will-change-transform',
      });
      words = split.words;
    } catch {
      if (paraRef.current) {
        words = Array.from(paraRef.current.querySelectorAll('.reveal-word'));
      }
    }

    if (words && words.length > 0) {
      gsap.set(words, { opacity: 0, rotate: 8, yPercent: 30 });
    }

    // Headline reveal. Anchored so it FINISHES while the headline is still
    // comfortably on screen -- the shipped `top 70% -> top -10%` ran the
    // scrub until the section was already leaving, so the words were still
    // resolving as they scrolled out from under the nav.
    const revealTl = gsap.timeline({
      scrollTrigger: {
        trigger: benefitRef.current,
        start: 'top 80%',
        end: 'top 20%',
        scrub: 1.2,
      },
    });

    if (words && words.length > 0) {
      revealTl.to(words, {
        stagger: 0.2,
        opacity: 1,
        rotate: 0,
        yPercent: 0,
        ease: 'power1.inOut',
      });
    }

    // The badges are anchored to their OWN row rather than sharing the
    // headline's timeline. They sit roughly 70vh below the section top, so on
    // the section's trigger they only entered the viewport as the scrub was
    // finishing -- which is what left them frozen mid-wipe with their text
    // cut off. Triggered on the row, they wipe open exactly as the row
    // crosses into view and are fully open well before it leaves.
    const tagTl = gsap.timeline({
      scrollTrigger: {
        trigger: tagRowRef.current,
        start: 'top 92%',
        end: 'top 55%',
        scrub: 1,
      },
    });

    tagRefs.current.forEach((tagEl) => {
      if (tagEl) {
        tagTl.to(
          tagEl,
          {
            duration: 1,
            opacity: 1,
            clipPath: 'polygon(0% 0%, 100% 0%, 100% 100%, 0% 100%)',
            ease: 'circ.out',
          },
          '>-0.75'
        );
      }
    });

    // Captions ride the same scrubbed pin timeline as the clip-path, so each
    // one surfaces and clears at a fixed point in the reveal. `at` is a
    // fraction of that timeline, which is why the clip tween below is given
    // an explicit duration of 1 -- GSAP's default is 0.5, and the fractions
    // would otherwise all land in the first half.
    const addCaptions = (timeline: gsap.core.Timeline) => {
      captionRefs.current.forEach((el, index) => {
        const caption = captions[index];
        if (!el || !caption) return;
        timeline
          .fromTo(
            el,
            { autoAlpha: 0, y: 28, filter: 'blur(10px)' },
            { autoAlpha: 1, y: 0, filter: 'blur(0px)', duration: 0.09, ease: 'power2.out' },
            caption.at
          )
          .to(
            el,
            { autoAlpha: 0, y: -28, filter: 'blur(10px)', duration: 0.09, ease: 'power2.in' },
            caption.at + 0.15
          );
      });
    };

    // ── Responsive MatchMedia for Small, Mid, and Large screens ─────────────
    const mm = gsap.matchMedia();

    // 1. Small Screens (Mobile < 640px)
    mm.add('(max-width: 639.9px)', () => {
      gsap.set(videoBoxRef.current, { clipPath: 'circle(18% at 50% 50%)' });

      const vpTl = gsap.timeline({
        scrollTrigger: {
          trigger: videoWrapperRef.current,
          start: 'top top',
          end: '+=1500',
          scrub: 1.2,
          pin: true,
          pinSpacing: true,
          anticipatePin: 1,
          onRefresh: paintPinBackground,
          onToggle: paintPinBackground,
        },
      });

      vpTl.fromTo(
        videoBoxRef.current,
        { clipPath: 'circle(18% at 50% 50%)' },
        { clipPath: 'circle(150% at 50% 50%)', ease: 'none', duration: 1 }
      );

      addCaptions(vpTl);
    });

    // 2. Mid Screens (Tablets / Phablets 640px - 1023.9px)
    mm.add('(min-width: 640px) and (max-width: 1023.9px)', () => {
      gsap.set(videoBoxRef.current, { clipPath: 'circle(12% at 50% 50%)' });

      const vpTl = gsap.timeline({
        scrollTrigger: {
          trigger: videoWrapperRef.current,
          start: 'top top',
          end: '+=2000',
          scrub: 1.3,
          pin: true,
          pinSpacing: true,
          anticipatePin: 1,
          onRefresh: paintPinBackground,
          onToggle: paintPinBackground,
        },
      });

      vpTl.fromTo(
        videoBoxRef.current,
        { clipPath: 'circle(12% at 50% 50%)' },
        { clipPath: 'circle(150% at 50% 50%)', ease: 'none', duration: 1 }
      );

      addCaptions(vpTl);
    });

    // 3. Large Screens (Desktop >= 1024px)
    mm.add('(min-width: 1024px)', () => {
      gsap.set(videoBoxRef.current, { clipPath: 'circle(8% at 50% 50%)' });

      const vpTl = gsap.timeline({
        scrollTrigger: {
          trigger: videoWrapperRef.current,
          start: 'top top',
          end: '+=2500',
          scrub: 1.5,
          pin: true,
          pinSpacing: true,
          anticipatePin: 1,
          onRefresh: paintPinBackground,
          onToggle: paintPinBackground,
        },
      });

      vpTl.fromTo(
        videoBoxRef.current,
        { clipPath: 'circle(8% at 50% 50%)' },
        { clipPath: 'circle(150% at 50% 50%)', ease: 'none', duration: 1 }
      );

      addCaptions(vpTl);
    });

    return () => {
      if (split && split.revert) split.revert();
      revealTl.kill();
      tagTl.kill();
      // mm.revert() already kills the pinned timelines this component made.
      // The blanket ScrollTrigger.getAll().forEach(kill) that was here also
      // killed other sections' triggers, which broke the page under React's
      // StrictMode double-mount.
      mm.revert();
      releaseSmoothScroll();
    };
  }, [captions]);

  return (
    <div
      className={`w-full min-h-screen text-[#e8ebf0] font-sans overflow-x-hidden ${className}`}
      style={{ backgroundColor: GROUND, color: '#e8ebf0' }}
    >
      <style
        dangerouslySetInnerHTML={{
          __html: `
            .pin-spacer {
              background-color: ${GROUND} !important;
            }
          `,
        }}
      />

      {/* ── Section 1: Intro Text ────────────────────────────────────────── */}
      <section
        className="w-full min-h-screen flex justify-center items-center text-center px-4 sm:px-8 py-8 text-[clamp(1.8rem,4.5vw,4.5rem)] font-bold tracking-tight leading-tight text-white relative z-10"
        style={{ backgroundColor: GROUND }}
      >
        {topText}
      </section>

      {/* ── Section 2: Benefit & Headline Section ─────────────────────────── */}
      <section
        ref={benefitRef}
        className="relative w-full min-h-[140vh] md:min-h-[160vh] pb-16 md:pb-20"
        style={{ backgroundColor: GROUND }}
      >
        <div
          className="max-w-5xl mx-auto px-4 sm:px-6 py-16 md:py-24 flex flex-col items-center text-center relative z-10"
          style={{ backgroundColor: GROUND }}
        >
          {/* Animated Kinetic Headline */}
          <div className="w-full mb-8 sm:mb-12 md:mb-14">
            <p
              ref={paraRef}
              className="text-[clamp(2rem,5vw,5rem)] font-extrabold tracking-tight leading-tight text-white overflow-visible"
            >
              {headingText}
            </p>
          </div>

          {/* Staggered Clip-Path Tag Badges */}
          <div
            ref={tagRowRef}
            className="flex flex-wrap justify-center gap-2.5 sm:gap-4 max-w-4xl mx-auto my-4 sm:my-6 mb-8 sm:mb-14"
          >
            {tags.map((tag, idx) => (
              <div
                key={tag.id || `tag-${idx}`}
                ref={(el) => {
                  tagRefs.current[idx] = el;
                }}
                // `whitespace-nowrap`: a pill is a fixed shape around one
                // phrase, and letting it wrap mid-wipe is what made
                // "Byte-identical replay" read as "Byte-identical".
                className="px-5 sm:px-8 py-2.5 sm:py-4 rounded-full text-[clamp(0.95rem,2vw,1.8rem)] font-semibold tracking-tight opacity-0 shadow-2xl whitespace-nowrap will-change-[clip-path,opacity]"
                style={{
                  backgroundColor: tag.background,
                  color: tag.color || '#e8ebf0',
                  clipPath: 'polygon(0% 0%, 0% 0%, 0% 100%, 0% 100%)',
                }}
              >
                {tag.text}
              </div>
            ))}
          </div>

          {subText && (
            <p className="text-[clamp(0.95rem,1.5vw,1.35rem)] text-zinc-400 font-normal max-w-xl mt-2 sm:mt-4 px-4">
              {subText}
            </p>
          )}
        </div>

        {/* ── Video Pin Section ───────────────────────────────────────────── */}
        <div className="relative w-full" style={{ backgroundColor: GROUND }}>
          <div
            ref={videoWrapperRef}
            className="w-full h-screen flex justify-center items-center relative overflow-hidden"
            style={{ backgroundColor: GROUND }}
          >
            {/* Absolute solid dark underlay behind the video expansion circle */}
            <div
              className="absolute inset-0 w-full h-full pointer-events-none"
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: '100%',
                height: '100%',
                backgroundColor: GROUND,
                zIndex: 1,
              }}
            />

            <div
              ref={videoBoxRef}
              className="relative w-full h-full overflow-hidden flex justify-center items-center will-change-[clip-path]"
              style={{ backgroundColor: GROUND, zIndex: 2 }}
            >
              {/* Rotating Circular Text Badge — off by default, see badgeImgSrc */}
              {badgeImgSrc && (
                <img
                  src={badgeImgSrc}
                  alt=""
                  className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-24 h-24 sm:w-32 sm:h-32 md:w-40 md:h-40 z-20 pointer-events-none animate-[spin_18s_linear_infinite] opacity-90 select-none"
                />
              )}

              {/* Drone Video */}
              <video
                ref={videoRef}
                autoPlay
                muted
                loop
                playsInline
                preload="auto"
                crossOrigin="anonymous"
                className="w-full h-full object-cover"
                style={{ width: '100%', height: '100%', objectFit: 'cover', backgroundColor: GROUND }}
              >
                <source src={videoSrc} type="video/mp4" />
              </video>

              {/* Scroll captions, placed around the frame rather than under
                  it. Each clears before the next arrives — see addCaptions(). */}
              {captions.map((caption, index) => (
                <div
                  key={caption.text}
                  ref={(el) => {
                    captionRefs.current[index] = el;
                  }}
                  className={`videocap videocap--${caption.position}${
                    caption.accent ? ' is-accent' : ''
                  }`}
                >
                  {caption.text}
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* ── Section 3: Bottom Outro Text ─────────────────────────────────── */}
      <section
        className="w-full min-h-screen flex justify-center items-center text-center px-4 sm:px-8 py-8 text-[clamp(1.8rem,4.5vw,4.5rem)] font-bold tracking-tight leading-tight text-white relative z-10"
        style={{ backgroundColor: GROUND }}
      >
        {bottomText}
      </section>
    </div>
  );
};

export default HeroScrollVideoReveal;

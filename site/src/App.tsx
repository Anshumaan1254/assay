/* The landing page, as one scroll.
 *
 *   1. MetroHero          the credit arrives — scroll is locked and drives
 *                         the video, then hands the page back
 *   2. LineStory          the argument, one line at a time, in and out
 *   3. HeroScrollVideoReveal  the recomputation, revealed out of a point
 *   4. ParallaxComponent  the ticker wall, in depth
 *   5. Closing            run it, and the way through to the reviewer
 *
 * RecursiveErosionBackground is mounted inside LineStory rather than behind
 * the whole page: the sections around it paint their own opaque ground for
 * the clip-path reveal to work against, so a page-wide field would have been
 * covered everywhere except the one beat that actually wants it -- and a
 * second iframe running a second render loop for nothing.
 */

import MetroHero from "@/components/ui/scroll-locked-video-hero";
import HeroScrollVideoReveal from "@/components/ui/hero-scroll-video-pin-reveal";
import { ParallaxComponent } from "@/components/ui/parallax-scrolling";
import LineStory from "@/components/LineStory";
import Closing from "@/components/Closing";
import { REPO, REVIEWER } from "@/lib/audit";

export default function App() {
  return (
    <>
      <header className="nav">
        <a className="nav__brand" href="#top">
          <span className="nav__mark">Assay</span>
          <span className="nav__sub">settlement audit engine</span>
        </a>
        <nav className="nav__links">
          <a href={REVIEWER}>Reviewer</a>
          <a className="nav__cta" href={REPO}>
            GitHub
          </a>
        </nav>
      </header>

      <main id="top">
        <MetroHero
          title="THE MONEY ARRIVES"
          tagline="You were told what was deducted. You were never shown."
          scrollHint="SCROLL"
          signature={false}
        />

        <LineStory />

        <HeroScrollVideoReveal />

        <ParallaxComponent
          title="Assay"
          caption="Decompose the credit. Reprice every deduction. Report what will not close."
        />

        <Closing />
      </main>
    </>
  );
}

"use client";

/*
 * Adapted from the "financial score cards" component.
 *
 * What changed, and why it had to: upstream, `handleGenerateScore()` called
 * `Utils.randomInt(0, max)` and the card displayed the result as a score,
 * with two more hardcoded at 42 and 83. On this page that would put invented
 * financial figures beside real, hash-verified rupee amounts with nothing
 * telling a reader which is which. The RNG is gone entirely -- there is no
 * `randomInt` in this file and no way to reach a generated number.
 *
 * Every value now arrives from /api/runs/{id}/scores, where it is derived in
 * reviewer/derive.py::score_cards as integer basis points over the persisted
 * report and unit-tested there. Each card shows the two real quantities the
 * ratio came from, so the gauge can be checked rather than trusted.
 *
 * The animation, gradient-stroke arc and staggered entrance are kept as-is.
 */

import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { CardContent, CardHeader, LiquidCard } from "@/components/ui/liquid-glass-card";
import { LiquidButton } from "@/components/ui/liquid-glass-button";
import type { ScoreCard } from "@/api";

type CounterContextType = { getNextIndex: () => number };
type StrengthColors = Record<string, string[]>;

const easeInOut = "cubic-bezier(0.65, 0, 0.35, 1)";

function circumference(r: number): number {
  return 2 * Math.PI * r;
}

function randomHash(length = 4): string {
  const chars = "abcdef0123456789";
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return [...bytes].map((b) => chars[b % chars.length]).join("");
}

const CounterContext = createContext<CounterContextType | undefined>(undefined);

const CounterProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const counterRef = useRef(0);
  const getNextIndex = useCallback(() => counterRef.current++, []);
  return <CounterContext.Provider value={{ getNextIndex }}>{children}</CounterContext.Provider>;
};

const useCounter = () => {
  const context = useContext(CounterContext);
  if (!context) throw new Error("useCounter must be used within a CounterProvider");
  return context.getNextIndex;
};

function ScoreCardShell({ children }: { children?: React.ReactNode }) {
  const getNextIndex = useCounter();
  const indexRef = useRef<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [appearing, setAppearing] = useState(false);

  if (indexRef.current === null) indexRef.current = getNextIndex();

  useEffect(() => {
    const delay = 300 + indexRef.current! * 200;
    timerRef.current = setTimeout(() => setAppearing(true), delay);
    return () => clearTimeout(timerRef.current);
  }, []);

  if (!appearing) return null;

  return (
    <LiquidCard className="h-full w-full animate-in fade-in slide-in-from-bottom-8 duration-800 fill-mode-both">
      <CardContent className="flex h-full flex-col p-7">{children}</CardContent>
    </LiquidCard>
  );
}

function ScoreDisplay({ card }: { card: ScoreCard }) {
  // The server's own preformatted percentage, rendered a character at a
  // time. Never a number the browser computed.
  const chars = card.headline.split("");

  return (
    // Absolutely positioned INSIDE the arc's hollow only. The detail line
    // used to ride along here too and collided with the arc stroke on any
    // card whose detail was long (two grouped rupee amounts, say); it now
    // sits below the gauge in normal flow, where it cannot overlap.
    <div className="pointer-events-none absolute inset-x-0 bottom-1 text-center">
      <div className="font-mono text-3xl font-medium tracking-tight leading-none">
        {chars.map((ch, i) => (
          <span
            key={i}
            className="inline-block animate-in slide-in-from-bottom-full fill-mode-both"
            style={{
              animationDelay: `${400 + i * 60}ms`,
              animationDuration: `${800 + i * 160}ms`,
            }}
          >
            {ch}
          </span>
        ))}
      </div>
    </div>
  );
}

function HalfCircle({ card }: { card: ScoreCard }) {
  const strokeRef = useRef<SVGCircleElement>(null);
  const gradIdRef = useRef(`grad-${randomHash()}`);
  const gradId = gradIdRef.current;
  const radius = 45;
  const distHalf = circumference(radius) / 2;
  const strokeDasharray = `${distHalf} ${distHalf}`;
  // value_bps is 0..10000; the arc sweeps that fraction of a half circle.
  const strokeDashoffset = -(Math.min(card.value_bps, 10_000) / 10_000) * distHalf;

  const strengthColors: StrengthColors = {
    none: ["hsl(220, 13%, 69%)", "hsl(220, 9%, 46%)"],
    weak: ["hsl(0, 84%, 80%)", "hsl(0, 84%, 60%)", "hsl(0, 84%, 40%)"],
    moderate: ["hsl(38, 92%, 80%)", "hsl(38, 92%, 60%)", "hsl(38, 92%, 40%)"],
    strong: ["hsl(142, 71%, 80%)", "hsl(142, 71%, 60%)", "hsl(142, 71%, 40%)"],
  };
  const colorStops = strengthColors[card.strength] ?? strengthColors.none;

  useEffect(() => {
    const duration = 1400;
    strokeRef.current?.animate(
      [
        { strokeDashoffset: "0", offset: 0 },
        { strokeDashoffset: "0", offset: 400 / duration },
        { strokeDashoffset: strokeDashoffset.toString() },
      ],
      { duration, easing: easeInOut, fill: "forwards" },
    );
  }, [strokeDashoffset]);

  return (
    <svg className="block mx-auto w-auto max-w-full h-36" viewBox="0 0 100 50" aria-hidden="true">
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="0">
          {colorStops.map((stop, i) => (
            <stop key={i} offset={`${(100 / (colorStops.length - 1)) * i}%`} stopColor={stop} />
          ))}
        </linearGradient>
      </defs>
      <g fill="none" strokeWidth="10" transform="translate(50, 50.5)">
        <circle className="stroke-muted/20" r={radius} />
        <circle
          ref={strokeRef}
          stroke={`url(#${gradId})`}
          strokeDasharray={strokeDasharray}
          r={radius}
        />
      </g>
    </svg>
  );
}

function ScoreHeader({ card }: { card: ScoreCard }) {
  const badgeClasses: Record<string, string> = {
    weak: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300",
    moderate: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-300",
    strong: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300",
  };
  const badgeClass = badgeClasses[card.strength] ?? "";

  return (
    // No `truncate`: the titles were being clipped ("Credits reconcilin…").
    // They are short enough to fit now that the CSS layering fix lets
    // `text-lg` actually apply, and wrapping beats an ellipsis anyway.
    <CardHeader className="flex flex-row items-start justify-between gap-3 pb-6 px-0 animate-in fade-in slide-in-from-bottom-12 duration-800">
      <h2 className="text-lg font-medium leading-snug">{card.title}</h2>
      <span
        className={`uppercase text-[0.625rem] font-semibold shrink-0 rounded-full px-2 py-0.5 tracking-wider ${badgeClass}`}
      >
        {card.strength}
      </span>
    </CardHeader>
  );
}

function Score({ card, onExplain }: { card: ScoreCard; onExplain?: (key: string) => void }) {
  return (
    <ScoreCardShell>
      <ScoreHeader card={card} />

      <div className="relative animate-in fade-in slide-in-from-bottom-12 duration-800">
        <HalfCircle card={card} />
        <ScoreDisplay card={card} />
      </div>

      <p className="mt-3 text-center font-mono text-xs text-muted-foreground">{card.detail}</p>

      <p className="mt-4 text-center text-sm leading-relaxed text-muted-foreground">
        {card.description}
      </p>

      {/* No "Calculate your score" button: there is nothing to calculate.
       * The number is already computed, signed and hashed by the engine.
       * mt-auto pins this to the bottom so buttons align across cards of
       * differing description length. */}
      <LiquidButton
        variant="default"
        size="default"
        onClick={() => onExplain?.(card.key)}
        className="mt-auto w-full h-12 pt-6 text-base animate-in fade-in slide-in-from-bottom-12 duration-800"
      >
        See the evidence
      </LiquidButton>
    </ScoreCardShell>
  );
}

export function FinancialScoreCards({
  cards,
  onExplain,
}: {
  cards: ScoreCard[];
  onExplain?: (key: string) => void;
}) {
  return (
    // A real grid, not wrapping flex: three max-w-md cards could not fit a
    // row, so the third dropped to its own centred line.
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3 items-stretch">
      <CounterProvider>
        {cards.map((card) => (
          <Score key={card.key} card={card} onExplain={onExplain} />
        ))}
      </CounterProvider>
    </div>
  );
}

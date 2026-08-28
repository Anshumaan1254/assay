/* eslint react/jsx-handler-names: "off" */
/*
 * Adapted from the visx area-chart-with-tooltip recipe.
 *
 * The upstream version plots `appleStock` from `@visx/mock-data`. That is
 * removed here, deliberately and non-negotiably: this component renders on
 * a settlement-audit page beside real, hash-verified rupee figures, and a
 * chart of invented money sitting inches from audited money -- with nothing
 * marking which is which -- is the exact failure this product exists to
 * prevent. `@visx/mock-data` is not even installed.
 *
 * Instead it plots the run's own bank credits: settled gross per credit
 * across the settlement month, from /api/runs/{id}/timeline. Amounts arrive
 * as exact integer paise and are converted to rupees only for the y-scale;
 * every number rendered as text comes from the server's own preformatted
 * string, so no rupee figure on screen is the result of browser float math.
 */
import React, { useCallback, useMemo } from "react";
import { AreaClosed, Bar, Line } from "@visx/shape";
import { curveMonotoneX } from "@visx/curve";
import { GridColumns, GridRows } from "@visx/grid";
import { scaleLinear, scaleTime } from "@visx/scale";
import {
  Tooltip,
  TooltipWithBounds,
  defaultStyles,
  withTooltip,
  type WithTooltipProvidedProps,
} from "@visx/tooltip";
// Imported from the package root, not the upstream snippet's deep
// `@visx/tooltip/lib/enhancers/withTooltip` path -- that is not a resolvable
// module in the installed version and fails the type check.
import { localPoint } from "@visx/event";
import { LinearGradient } from "@visx/gradient";
import { bisector, extent, max } from "@visx/vendor/d3-array";
import { timeFormat } from "@visx/vendor/d3-time-format";

import type { TimelinePoint } from "@/api";

type TooltipData = TimelinePoint;

export const background = "#0d0f13";
export const background2 = "#08090b";
export const accentColor = "#e8b44a";
export const accentColorDark = "#75daad";

const tooltipStyles = {
  ...defaultStyles,
  background: "#12151a",
  border: "1px solid #2a313d",
  color: "#e8ebf0",
  fontFamily: '"JetBrains Mono", ui-monospace, monospace',
  fontSize: "0.75rem",
};

const formatDate = timeFormat("%b %d, '%y");

const getDate = (d: TimelinePoint) => new Date(d.value_date);
/* paise -> rupees for the scale only. Integer division keeps this off the
 * fractional-float path; the label text never comes from here. */
const getValue = (d: TimelinePoint) => d.settled_gross.paise / 100;
const bisectDate = bisector<TimelinePoint, Date>((d) => new Date(d.value_date)).left;

export type ComponentProps = {
  data: TimelinePoint[];
  width: number;
  height: number;
  margin?: { top: number; right: number; bottom: number; left: number };
};

const ComponentLogic = ({
  data,
  width,
  height,
  margin = { top: 0, right: 0, bottom: 0, left: 0 },
  showTooltip,
  hideTooltip,
  tooltipData,
  tooltipTop = 0,
  tooltipLeft = 0,
}: ComponentProps & WithTooltipProvidedProps<TooltipData>) => {
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  const dateScale = useMemo(
    () =>
      scaleTime({
        range: [margin.left, innerWidth + margin.left],
        domain: extent(data, getDate) as [Date, Date],
      }),
    [data, innerWidth, margin.left],
  );

  const valueScale = useMemo(
    () =>
      scaleLinear({
        range: [innerHeight + margin.top, margin.top],
        domain: [0, (max(data, getValue) || 0) * 1.25],
        nice: true,
      }),
    [data, margin.top, innerHeight],
  );

  const handleTooltip = useCallback(
    (event: React.TouchEvent<SVGRectElement> | React.MouseEvent<SVGRectElement>) => {
      const { x } = localPoint(event) || { x: 0 };
      const x0 = dateScale.invert(x);
      const index = bisectDate(data, x0, 1);
      const d0 = data[index - 1];
      const d1 = data[index];
      let d = d0;
      if (d1 && getDate(d1)) {
        d = x0.valueOf() - getDate(d0).valueOf() > getDate(d1).valueOf() - x0.valueOf() ? d1 : d0;
      }
      if (!d) return;
      showTooltip({
        tooltipData: d,
        tooltipLeft: x,
        tooltipTop: valueScale(getValue(d)),
      });
    },
    [data, showTooltip, valueScale, dateScale],
  );

  // Guard AFTER the hooks, never before: an early return above a useMemo
  // changes hook order between renders and React will throw.
  if (width < 10 || data.length === 0) return null;

  return (
    <div>
      <svg width={width} height={height}>
        <rect x={0} y={0} width={width} height={height} fill="url(#area-background-gradient)" rx={14} />
        <LinearGradient id="area-background-gradient" from={background} to={background2} />
        <LinearGradient id="area-gradient" from={accentColor} to={accentColor} toOpacity={0.08} />
        <GridRows
          left={margin.left}
          scale={valueScale}
          width={innerWidth}
          strokeDasharray="1,3"
          stroke={accentColor}
          strokeOpacity={0.12}
          pointerEvents="none"
        />
        <GridColumns
          top={margin.top}
          scale={dateScale}
          height={innerHeight}
          strokeDasharray="1,3"
          stroke={accentColor}
          strokeOpacity={0.12}
          pointerEvents="none"
        />
        <AreaClosed<TimelinePoint>
          data={data}
          x={(d) => dateScale(getDate(d)) ?? 0}
          y={(d) => valueScale(getValue(d)) ?? 0}
          yScale={valueScale}
          strokeWidth={1.5}
          stroke={accentColor}
          fill="url(#area-gradient)"
          curve={curveMonotoneX}
        />
        <Bar
          x={margin.left}
          y={margin.top}
          width={innerWidth}
          height={innerHeight}
          fill="transparent"
          rx={14}
          onTouchStart={handleTooltip}
          onTouchMove={handleTooltip}
          onMouseMove={handleTooltip}
          onMouseLeave={() => hideTooltip()}
        />
        {tooltipData && (
          <g>
            <Line
              from={{ x: tooltipLeft, y: margin.top }}
              to={{ x: tooltipLeft, y: innerHeight + margin.top }}
              stroke={accentColorDark}
              strokeWidth={2}
              pointerEvents="none"
              strokeDasharray="5,2"
            />
            <circle
              cx={tooltipLeft}
              cy={tooltipTop + 1}
              r={4}
              fill="black"
              fillOpacity={0.1}
              stroke="black"
              strokeOpacity={0.1}
              strokeWidth={2}
              pointerEvents="none"
            />
            <circle
              cx={tooltipLeft}
              cy={tooltipTop}
              r={4}
              fill={accentColorDark}
              stroke="white"
              strokeWidth={2}
              pointerEvents="none"
            />
          </g>
        )}
      </svg>
      {tooltipData && (
        <div>
          <TooltipWithBounds top={tooltipTop - 12} left={tooltipLeft + 12} style={tooltipStyles}>
            {/* The server's own string, not a client-side format of a float. */}
            <div>₹{tooltipData.settled_gross.rupees}</div>
            <div style={{ color: "#8b94a3", marginTop: 2 }}>{tooltipData.credit_id}</div>
            {tooltipData.unexplained.paise !== 0 && (
              <div style={{ color: "#ff6b6b", marginTop: 2 }}>
                unexplained ₹{tooltipData.unexplained.rupees}
              </div>
            )}
          </TooltipWithBounds>
          <Tooltip
            top={innerHeight + margin.top - 14}
            left={tooltipLeft}
            style={{
              ...defaultStyles,
              ...tooltipStyles,
              minWidth: 84,
              textAlign: "center",
              transform: "translateX(-50%)",
            }}
          >
            {formatDate(getDate(tooltipData))}
          </Tooltip>
        </div>
      )}
    </div>
  );
};

export const Component = withTooltip<ComponentProps, TooltipData>(ComponentLogic);

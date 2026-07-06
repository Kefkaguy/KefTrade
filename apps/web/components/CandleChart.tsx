"use client";

import type { Candle } from "@/lib/api";

type CandleChartProps = {
  candles: Candle[];
};

export function CandleChart({ candles }: CandleChartProps) {
  const visible = candles.slice(-90);
  if (visible.length < 2) {
    return <div className="chart" />;
  }

  const highs = visible.map((candle) => Number(candle.high));
  const lows = visible.map((candle) => Number(candle.low));
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const width = 900;
  const height = 330;
  const pad = 22;
  const step = (width - pad * 2) / visible.length;
  const scaleY = (price: number) => {
    if (max === min) return height / 2;
    return pad + ((max - price) / (max - min)) * (height - pad * 2);
  };

  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Development asset candlestick chart">
      {visible.map((candle, index) => {
        const open = Number(candle.open);
        const close = Number(candle.close);
        const high = Number(candle.high);
        const low = Number(candle.low);
        const x = pad + index * step + step / 2;
        const yOpen = scaleY(open);
        const yClose = scaleY(close);
        const color = close >= open ? "#16735f" : "#a33a3a";
        const bodyTop = Math.min(yOpen, yClose);
        const bodyHeight = Math.max(2, Math.abs(yClose - yOpen));
        return (
          <g key={`${candle.timestamp}-${index}`}>
            <line x1={x} x2={x} y1={scaleY(high)} y2={scaleY(low)} stroke={color} strokeWidth="1.5" />
            <rect x={x - Math.max(2, step * 0.28)} y={bodyTop} width={Math.max(4, step * 0.56)} height={bodyHeight} fill={color} rx="1" />
          </g>
        );
      })}
    </svg>
  );
}

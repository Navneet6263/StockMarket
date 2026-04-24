"use client";

import { ColorType, CrosshairMode, UTCTimestamp, createChart } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { Candle, MarketSignal } from "../lib/market";

type Props = {
  symbol: string;
  candles: Candle[];
  prediction: MarketSignal;
  quote: {
    price: number;
    change_percent: number;
    volume: number;
    timestamp: string;
  };
};

type ChartApi = ReturnType<typeof createChart>;
type CandleSeriesApi = ReturnType<ChartApi["addCandlestickSeries"]>;
type VolumeSeriesApi = ReturnType<ChartApi["addHistogramSeries"]>;
type PriceLineApi = ReturnType<CandleSeriesApi["createPriceLine"]>;

export default function LiveChart({ symbol, candles, prediction, quote }: Props) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ChartApi | null>(null);
  const candleSeriesRef = useRef<CandleSeriesApi | null>(null);
  const volumeSeriesRef = useRef<VolumeSeriesApi | null>(null);
  const priceLinesRef = useRef<PriceLineApi[]>([]);

  useEffect(() => {
    if (!chartContainerRef.current || chartRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#09111f" },
        textColor: "#d8e2ef",
      },
      grid: {
        vertLines: { color: "#162437" },
        horzLines: { color: "#162437" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      width: chartContainerRef.current.clientWidth,
      height: 360,
    });

    chartRef.current = chart;
    candleSeriesRef.current = chart.addCandlestickSeries({
      upColor: "#2dd4bf",
      downColor: "#fb7185",
      borderVisible: false,
      wickUpColor: "#2dd4bf",
      wickDownColor: "#fb7185",
    });
    volumeSeriesRef.current = chart.addHistogramSeries({
      color: "#2dd4bf",
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });

    volumeSeriesRef.current.priceScale().applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    const handleResize = () => {
      if (chartContainerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: chartContainerRef.current.clientWidth });
      }
    };

    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      priceLinesRef.current = [];
    };
  }, []);

  useEffect(() => {
    if (!candleSeriesRef.current || !volumeSeriesRef.current || !candles.length) return;
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;

    candleSeries.setData(
      candles.map((item) => ({
        time: item.time as UTCTimestamp,
        open: item.open,
        high: item.high,
        low: item.low,
        close: item.close,
      }))
    );
    volumeSeries.setData(
      candles.map((item) => ({
        time: item.time as UTCTimestamp,
        value: item.volume,
        color: item.close >= item.open ? "#2dd4bf" : "#fb7185",
      }))
    );

    priceLinesRef.current.forEach((line) => candleSeries.removePriceLine(line));
    priceLinesRef.current = [];

    const levels = [
      { price: prediction.support, color: "#34d399", title: "Support" },
      { price: prediction.resistance, color: "#f59e0b", title: "Resistance" },
      { price: prediction.invalidation, color: "#fb7185", title: "Invalidation" },
    ];
    levels.forEach((level) => {
      if (!level.price) return;
      const line = candleSeries.createPriceLine({
        price: level.price,
        color: level.color,
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: level.title,
      });
      priceLinesRef.current.push(line);
    });

    chartRef.current?.timeScale().fitContent();
  }, [symbol, candles, prediction.support, prediction.resistance, prediction.invalidation]);

  return (
    <div className="chart-shell">
      <div className="chart-toolbar">
        <div>
          <div className="mono-label">{symbol}</div>
          <h3>Live tape and structure map</h3>
        </div>
        <div className="price-stack" style={{ textAlign: "right" }}>
          <div className="price-tag">INR {quote.price.toFixed(2)}</div>
          <div style={{ color: quote.change_percent >= 0 ? "#34d399" : "#fb7185" }}>
            {quote.change_percent >= 0 ? "+" : ""}
            {quote.change_percent.toFixed(2)}%
          </div>
          <div className="micro-copy">Vol {quote.volume.toLocaleString()}</div>
        </div>
      </div>
      <div ref={chartContainerRef} style={{ width: "100%", minHeight: 420 }} />
    </div>
  );
}

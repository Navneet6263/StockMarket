"use client";
import { useEffect, useRef } from "react";
import { createChart, ColorType, CrosshairMode, SeriesMarker, Time } from "lightweight-charts";
import { API_URL } from "../lib/market";

interface TradingChartProps {
  symbol: string;
  data: any; // Now contains { data: [], active_zones: [] }
  prediction?: any;
}

export default function TradingChart({ symbol, data, prediction }: TradingChartProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<any>(null);

  useEffect(() => {
    const chartData = data?.data || [];
    const zones = data?.active_zones || [];
    if (!chartContainerRef.current || chartData.length === 0) return;

    const handleResize = () => {
      if (chartRef.current && chartContainerRef.current) {
        chartRef.current.applyOptions({ width: chartContainerRef.current.clientWidth });
      }
    };

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "rgba(255, 255, 255, 0.7)",
      },
      grid: {
        vertLines: { color: "rgba(255, 255, 255, 0.05)" },
        horzLines: { color: "rgba(255, 255, 255, 0.05)" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: "rgba(255, 255, 255, 0.1)",
      },
      timeScale: {
        borderColor: "rgba(255, 255, 255, 0.1)",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    chartRef.current = chart;

    const candlestickSeries = chart.addCandlestickSeries({
      upColor: "#10b981",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });

    // Sort data chronologically before mapping
    const sortedData = [...chartData].sort((a, b) => {
      const timeA = new Date(a.date || a.timestamp).getTime();
      const timeB = new Date(b.date || b.timestamp).getTime();
      return timeA - timeB;
    });

    const formattedData = sortedData.map((d: any) => ({
      time: (new Date(d.date || d.timestamp).getTime() / 1000) as Time,
      open: Number(d.open),
      high: Number(d.high),
      low: Number(d.low),
      close: Number(d.close),
    }));
    
    candlestickSeries.setData(formattedData);

    // Add Markers for Traps / Signals
    if (prediction && formattedData.length > 0) {
      const markers: SeriesMarker<any>[] = [];
      const lastCandle = formattedData[formattedData.length - 1];

      // Bull Trap Marker
      if (prediction.trap_risk === "high" || prediction.bull_trap?.bull_trap_detected) {
        markers.push({
          time: lastCandle.time,
          position: "aboveBar",
          color: "#ef4444",
          shape: "arrowDown",
          text: "🚨 BULL TRAP",
        });
      } else if (prediction.direction === "bullish" && prediction.confidence >= 70) {
        markers.push({
          time: lastCandle.time,
          position: "belowBar",
          color: "#10b981",
          shape: "arrowUp",
          text: "🚀 BUY",
        });
      } else if (prediction.direction === "bearish" && prediction.confidence >= 70) {
        markers.push({
          time: lastCandle.time,
          position: "aboveBar",
          color: "#ef4444",
          shape: "arrowDown",
          text: "🔻 SELL",
        });
      }
      
      if (markers.length > 0) {
        candlestickSeries.setMarkers(markers);
      }
    }

    // Add Support/Resistance lines if available
    if (prediction?.entry_trigger) {
      candlestickSeries.createPriceLine({
        price: prediction.entry_trigger,
        color: "#3b82f6",
        lineWidth: 2,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "Entry",
      });
    }
    if (prediction?.target_1 || prediction?.target_price) {
      candlestickSeries.createPriceLine({
        price: prediction.target_1 || prediction.target_price,
        color: "#10b981",
        lineWidth: 2,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "Target 1",
      });
    }
    if (prediction?.stop_loss || prediction?.invalidation) {
      candlestickSeries.createPriceLine({
        price: prediction.stop_loss || prediction.invalidation,
        color: "#ef4444",
        lineWidth: 2,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "Stop Loss",
      });
    }

    // Render GTF Demand & Supply Zones
    if (zones && zones.length > 0) {
      zones.forEach((z: any) => {
        const color = z.type === "demand" ? "rgba(16, 185, 129, 0.4)" : "rgba(239, 68, 68, 0.4)";
        const title = z.type === "demand" ? "Demand Zone" : "Supply Zone";
        
        candlestickSeries.createPriceLine({
          price: z.proximal,
          color: color,
          lineWidth: 1,
          lineStyle: 0,
          axisLabelVisible: true,
          title: title,
        });
        
        candlestickSeries.createPriceLine({
          price: z.distal,
          color: color,
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: false,
          title: "",
        });
      });
    }

    chart.timeScale().fitContent();
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, [data, prediction]);

  return (
    <div 
      ref={chartContainerRef} 
      style={{ width: "100%", height: "400px", marginTop: "16px", borderRadius: "12px", overflow: "hidden", background: "rgba(0,0,0,0.2)" }} 
    />
  );
}

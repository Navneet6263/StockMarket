"use client";
import { useEffect, useRef } from "react";
import { createChart, ColorType, CrosshairMode, SeriesMarker, Time } from "lightweight-charts";
import { API_URL } from "../lib/market";

interface TradingChartProps {
  symbol: string;
  data: any; // Now contains { data: [], active_zones: [] }
  prediction?: any;
  liveQuote?: any;
}

export default function TradingChart({ symbol, data, prediction, liveQuote }: TradingChartProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<any>(null);
  const seriesRef = useRef<any>(null);
  const lastCandleTimeRef = useRef<any>(null);
  const lastUpdateRef = useRef<number>(0);     // throttle: epoch ms of last chart update
  const lastWsCandle = useRef<any>(null);      // hold latest candle between throttle windows

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
    seriesRef.current = candlestickSeries;

    // Sort data chronologically before mapping
    const sortedData = [...chartData].sort((a, b) => {
      const timeA = a.time ? a.time * 1000 : new Date(a.date || a.timestamp).getTime();
      const timeB = b.time ? b.time * 1000 : new Date(b.date || b.timestamp).getTime();
      return timeA - timeB;
    });

    const formattedData = sortedData.map((d: any) => {
      const timestamp = d.time ? d.time : (new Date(d.date || d.timestamp).getTime() / 1000);
      return {
        time: timestamp as Time,
        open: Number(d.open),
        high: Number(d.high),
        low: Number(d.low),
        close: Number(d.close),
      };
    });
    
    candlestickSeries.setData(formattedData);

    // Add Markers for Traps / Signals
    if (prediction && formattedData.length > 0) {
      const markers: SeriesMarker<any>[] = [];
      const lastCandle = formattedData[formattedData.length - 1];
      lastCandleTimeRef.current = lastCandle.time;

      // Bull Trap Marker
      if (prediction.trap_risk === "high" || prediction.bull_trap?.bull_trap_detected) {
        markers.push({
          time: lastCandle.time,
          position: "aboveBar",
          color: "#ef4444",
          shape: "arrowDown",
          text: "🚨 BULL TRAP",
        });
      } else if (prediction.direction === "bullish" && (prediction.action === "BUY" || prediction.action === "REENTRY_BUY" || prediction.action === "VALID_BREAKOUT")) {
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
    const entryLinePrice = prediction?.safe_entry_price || prediction?.entry_price || prediction?.entry_trigger;
    if (entryLinePrice) {
      candlestickSeries.createPriceLine({
        price: entryLinePrice,
        color: "#3b82f6",
        lineWidth: 2,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "Entry",
      });
    }
    const targetPrice = prediction?.new_target || prediction?.target_1 || prediction?.target_price;
    if (targetPrice) {
      candlestickSeries.createPriceLine({
        price: targetPrice,
        color: "#10b981",
        lineWidth: 2,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "Target 1",
      });
    }
    
    const stopLossPrice = prediction?.invalidation_level || prediction?.invalidation || prediction?.stop_loss;
    if (stopLossPrice) {
      candlestickSeries.createPriceLine({
        price: stopLossPrice,
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

  // Real-time WebSocket connection for live candle and pattern updates
  useEffect(() => {
    if (!symbol || !seriesRef.current) return;
    
    const wsProtocol = window.location.protocol === "https:" || API_URL.startsWith("https") ? "wss:" : "ws:";
    const host = API_URL.replace(/^https?:\/\//, "");
    const wsUrl = `${wsProtocol}//${host}/api/live/ws/chart/${symbol}`;
    
    const ws = new WebSocket(wsUrl);
    
    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);

        if (payload.type === "CANDLE_UPDATE") {
          const c = payload.data;

          // ── Fix: ensure time is a plain number (Unix seconds) ──────────────
          let candleTime: number;
          if (typeof c.time === "number") {
            candleTime = c.time;
          } else if (typeof c.time === "object" && c.time !== null) {
            // lightweight-charts BusinessDay object { year, month, day }
            const bd = c.time as { year: number; month: number; day: number };
            candleTime = Math.floor(new Date(bd.year, bd.month - 1, bd.day).getTime() / 1000);
          } else if (typeof c.time === "string") {
            candleTime = Math.floor(new Date(c.time).getTime() / 1000);
          } else {
            return; // skip unparseable
          }

          // ── Reject candles older than the last known candle ──────────────
          if (lastCandleTimeRef.current && candleTime < lastCandleTimeRef.current) {
            return;
          }

          const candle = {
            time: candleTime as Time,
            open:  Number(c.open),
            high:  Number(c.high),
            low:   Number(c.low),
            close: Number(c.close),
          };

          // ── Throttle: buffer update, apply max once per second ──────────
          lastWsCandle.current = candle;
          const now = Date.now();
          if (now - lastUpdateRef.current >= 1000) {
            lastUpdateRef.current = now;
            if (seriesRef.current) {
              try {
                seriesRef.current.update(candle);
                lastCandleTimeRef.current = candleTime;
              } catch (_) { /* ignore out-of-order */ }
            }
          }
        }

        if (payload.type === "PATTERN_DETECTED") {
          const currentMarkers = seriesRef.current?.markers() || [];
          const newMarkers = payload.patterns.map((p: any) => {
            let marker: SeriesMarker<any> = { time: p.time as Time, position: 'aboveBar', shape: 'arrowDown', text: p.message, color: '' };
            switch(p.type) {
              case "INSTITUTIONAL_TRAP":
              case "BULL_TRAP":
                marker.color = "#ef4444";
                marker.text = "🚨 " + p.message;
                break;
              case "BEAR_TRAP":
                marker.color = "#10b981";
                marker.position = "belowBar";
                marker.shape = "arrowUp";
                marker.text = "🟢 " + p.message;
                break;
              case "ACCUMULATION":
                marker.color = "#3b82f6";
                marker.position = "belowBar";
                marker.shape = "arrowUp";
                marker.text = "🐋 " + p.message;
                break;
              case "VOLUME_ANOMALY":
                marker.color = "#f59e0b";
                marker.position = "belowBar";
                marker.shape = "circle";
                marker.text = "🔥 Vol " + p.message;
                break;
            }
            return marker;
          });
          if (seriesRef.current) {
            seriesRef.current.setMarkers([...currentMarkers, ...newMarkers]);
          }
        }
      } catch (err) {
        // silently suppress — don't log on every bad tick
      }
    };
    
    return () => ws.close();
  }, [symbol]);

  return (
    <div 
      ref={chartContainerRef} 
      style={{ width: "100%", height: "400px", marginTop: "16px", borderRadius: "12px", overflow: "hidden", background: "rgba(0,0,0,0.2)" }} 
    />
  );
}

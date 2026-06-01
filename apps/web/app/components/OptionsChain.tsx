"use client";

import React, { useEffect, useState } from "react";

interface StrikeData {
  strike: number;
  ce_ltp: number;
  ce_oi: number;
  ce_volume: number;
  ce_iv: number;
  pe_ltp: number;
  pe_oi: number;
  pe_volume: number;
  pe_iv: number;
  itm: boolean;
  institutional_flag: boolean;
}

interface OptionsSnapshot {
  type: string;
  symbol: string;
  expiry: string;
  pcr: number;
  max_pain: number;
  spot_price: number;
  strikes: StrikeData[];
}

export default function OptionsChain({ symbol }: { symbol: string }) {
  const [snapshot, setSnapshot] = useState<OptionsSnapshot | null>(null);

  useEffect(() => {
    if (!symbol) return;
    
    // Connect to options websocket
    const ws = new WebSocket(`ws://localhost:8000/api/live/ws/options/${symbol}`);
    
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "OPTIONS_SNAPSHOT") {
          setSnapshot(data);
        }
      } catch (err) {
        console.error("Options WS parse error", err);
      }
    };
    
    return () => {
      ws.close();
    };
  }, [symbol]);

  if (!snapshot) {
    return <div className="p-4 text-gray-500 animate-pulse">Loading Live Options Chain for {symbol}...</div>;
  }

  // Find max OI for the bar charts
  let maxOi = 1;
  snapshot.strikes.forEach(s => {
    if (s.ce_oi > maxOi) maxOi = s.ce_oi;
    if (s.pe_oi > maxOi) maxOi = s.pe_oi;
  });

  return (
    <div className="flex flex-col h-full bg-white rounded-md border border-gray-200 overflow-hidden text-sm">
      <div className="flex justify-between items-center bg-gray-50 p-3 border-b border-gray-200 font-semibold">
        <div>
          <span className="text-gray-800 mr-2">{snapshot.symbol} Option Chain</span>
          <span className="text-xs text-gray-500 font-normal">Exp: {snapshot.expiry}</span>
        </div>
        <div className="flex gap-4">
          <div className="flex items-center gap-1">
            <span className="text-gray-500 text-xs uppercase tracking-wider">Spot</span>
            <span className="font-mono">{snapshot.spot_price.toFixed(2)}</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="text-gray-500 text-xs uppercase tracking-wider">Max Pain</span>
            <span className="text-purple-700 font-mono font-bold">{snapshot.max_pain}</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="text-gray-500 text-xs uppercase tracking-wider">PCR</span>
            <span className={`font-mono font-bold ${snapshot.pcr > 1 ? "text-green-600" : snapshot.pcr < 1 ? "text-red-600" : "text-gray-700"}`}>
              {snapshot.pcr.toFixed(2)}
            </span>
          </div>
        </div>
      </div>
      
      <div className="overflow-y-auto flex-1">
        <table className="w-full text-right border-collapse">
          <thead className="bg-gray-100 sticky top-0 shadow-sm z-10 text-xs text-gray-600">
            <tr>
              <th className="p-2 font-medium text-center" colSpan={4}>CALLS</th>
              <th className="p-2 font-medium text-center bg-gray-200 border-x border-gray-300">STRIKE</th>
              <th className="p-2 font-medium text-center" colSpan={4}>PUTS</th>
            </tr>
            <tr className="border-b border-gray-200">
              <th className="p-2 font-medium w-16">IV</th>
              <th className="p-2 font-medium w-32 text-center">OI (Lakhs)</th>
              <th className="p-2 font-medium w-16">Vol</th>
              <th className="p-2 font-medium w-16">LTP</th>
              <th className="p-2 font-medium w-20 text-center bg-gray-200 border-x border-gray-300">₹</th>
              <th className="p-2 font-medium w-16">LTP</th>
              <th className="p-2 font-medium w-16">Vol</th>
              <th className="p-2 font-medium w-32 text-center">OI (Lakhs)</th>
              <th className="p-2 font-medium w-16">IV</th>
            </tr>
          </thead>
          <tbody className="font-mono text-[13px]">
            {snapshot.strikes.map(s => {
              const isMaxPain = s.strike === snapshot.max_pain;
              // ITM logic: CE is ITM if strike < spot, PE is ITM if strike > spot
              const ceItm = snapshot.spot_price > 0 && s.strike < snapshot.spot_price;
              const peItm = snapshot.spot_price > 0 && s.strike > snapshot.spot_price;
              
              const ceOiPct = (s.ce_oi / maxOi) * 100;
              const peOiPct = (s.pe_oi / maxOi) * 100;
              
              return (
                <tr key={s.strike} className={`border-b border-gray-100 hover:bg-gray-50 transition-colors ${isMaxPain ? 'border-y-2 border-y-purple-500 bg-purple-50/30' : ''}`}>
                  {/* CALLS */}
                  <td className={`p-2 ${ceItm ? 'bg-yellow-50' : ''}`}>{(s.ce_iv || 0).toFixed(1)}</td>
                  <td className={`p-2 relative group ${ceItm ? 'bg-yellow-50' : ''}`}>
                    <div className="absolute top-1/2 -translate-y-1/2 right-2 h-4 bg-red-100 rounded-sm overflow-hidden" style={{ width: '80%', opacity: 0.6 }}>
                      <div className="h-full bg-red-400 float-right" style={{ width: `${ceOiPct}%` }}></div>
                    </div>
                    <span className="relative z-10 pr-2 block">
                      {s.institutional_flag && <span className="mr-1" title="Institutional Activity (OI Spike > 20%)">🚨</span>}
                      {(s.ce_oi / 100000).toFixed(2)}
                    </span>
                  </td>
                  <td className={`p-2 ${ceItm ? 'bg-yellow-50' : ''}`}>{s.ce_volume}</td>
                  <td className={`p-2 font-semibold ${ceItm ? 'bg-yellow-50 text-gray-900' : 'text-gray-600'}`}>{s.ce_ltp.toFixed(2)}</td>
                  
                  {/* STRIKE */}
                  <td className={`p-2 text-center font-bold bg-gray-100 border-x border-gray-200 ${isMaxPain ? 'text-purple-700 bg-purple-100' : 'text-gray-800'}`}>
                    {s.strike}
                  </td>
                  
                  {/* PUTS */}
                  <td className={`p-2 font-semibold ${peItm ? 'bg-yellow-50 text-gray-900' : 'text-gray-600'}`}>{s.pe_ltp.toFixed(2)}</td>
                  <td className={`p-2 ${peItm ? 'bg-yellow-50' : ''}`}>{s.pe_volume}</td>
                  <td className={`p-2 relative text-left group ${peItm ? 'bg-yellow-50' : ''}`}>
                    <div className="absolute top-1/2 -translate-y-1/2 left-2 h-4 bg-green-100 rounded-sm overflow-hidden" style={{ width: '80%', opacity: 0.6 }}>
                      <div className="h-full bg-green-400 float-left" style={{ width: `${peOiPct}%` }}></div>
                    </div>
                    <span className="relative z-10 pl-2 block">
                      {(s.pe_oi / 100000).toFixed(2)}
                      {s.institutional_flag && <span className="ml-1" title="Institutional Activity (OI Spike > 20%)">🚨</span>}
                    </span>
                  </td>
                  <td className={`p-2 ${peItm ? 'bg-yellow-50' : ''}`}>{(s.pe_iv || 0).toFixed(1)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

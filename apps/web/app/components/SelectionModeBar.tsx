"use client";

type SelectionModeBarProps = {
  activeDetailSymbol: string;
  scannerHighlightedSymbol: string;
  selectedSymbol: string | null;
  onFollowScanner: () => void;
};

export default function SelectionModeBar({
  activeDetailSymbol,
  scannerHighlightedSymbol,
  selectedSymbol,
  onFollowScanner,
}: SelectionModeBarProps) {
  const followingScanner = !selectedSymbol;

  return (
    <div className="selection-mode-bar">
      <div className={`selection-pill ${followingScanner ? "is-following" : "is-pinned"}`}>
        {followingScanner
          ? `Following scanner leader${scannerHighlightedSymbol ? `: ${scannerHighlightedSymbol}` : ""}`
          : `Pinned selection: ${selectedSymbol}`}
      </div>
      <div className="micro-copy">
        Active detail symbol: {activeDetailSymbol || "waiting for first eligible scan result"}
      </div>
      {!followingScanner && scannerHighlightedSymbol ? (
        <button type="button" className="ghost-button compact" onClick={onFollowScanner}>
          Return To Scanner Leader
        </button>
      ) : null}
    </div>
  );
}

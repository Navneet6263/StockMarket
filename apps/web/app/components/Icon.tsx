// Minimal stroke icon set (24×24, 1.75px stroke) for navigation.
const PATHS: Record<string, string[]> = {
  home: ["M3 10.5 12 3l9 7.5", "M5 9.5V21h14V9.5", "M10 21v-6h4v6"],
  live: ["M3 12h4l3-8 4 16 3-8h4"],
  star: ["M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"],
  volume: ["M4 20V10", "M10 20V4", "M16 20v-7", "M22 20H2"],
  base: ["M12 3 2 8l10 5 10-5-10-5z", "M2 13l10 5 10-5", "M2 18l10 5 10-5"],
  momentum: ["M3 17l6-6 4 4 8-8", "M15 7h6v6"],
  zones: ["M3 5h18v4H3z", "M3 15h18v4H3z", "M7 9v6", "M17 9v6"],
  eye: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"],
  alert: ["M12 3 2 21h20L12 3z", "M12 10v5", "M12 18h.01"],
  options: ["M13 2 4 14h7l-1 8 9-12h-7l1-8z"],
  briefcase: ["M3 7h18v13H3z", "M8 7V4h8v3", "M3 12h18"],
};

export default function Icon({ name, size = 16 }: { name: string; size?: number }) {
  const paths = PATHS[name] || PATHS.home;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {paths.map((d, i) => <path key={i} d={d} />)}
    </svg>
  );
}

// Small inline stroke icons (no icon font, no external assets).
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function make(paths: string[], displayName: string) {
  const Icon = ({ size = 18, ...props }: IconProps) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
      {paths.map((d) => <path key={d} d={d} />)}
    </svg>
  );
  Icon.displayName = displayName;
  return Icon;
}

export const IconDashboard = make(["M4 13h6V4H4z", "M14 20h6v-9h-6z", "M4 20h6v-4H4z", "M14 8h6V4h-6z"], "Dashboard");
export const IconSpark = make(["M12 3v4", "M12 17v4", "M3 12h4", "M17 12h4", "M6.3 6.3l2.5 2.5", "M15.2 15.2l2.5 2.5",
  "M6.3 17.7l2.5-2.5", "M15.2 8.8l2.5-2.5"], "Spark");
export const IconList = make(["M8 6h12", "M8 12h12", "M8 18h12", "M4 6h.01", "M4 12h.01", "M4 18h.01"], "List");
export const IconSplit = make(["M6 3v12", "M18 9v12", "M6 15a6 6 0 0 0 6 6h6", "M18 9a6 6 0 0 0-6-6H6"], "Split");
export const IconPulse = make(["M3 12h4l3-8 4 16 3-8h4"], "Pulse");
export const IconSliders = make(["M4 21v-7", "M4 10V3", "M12 21v-9", "M12 8V3", "M20 21v-5", "M20 12V3", "M1 14h6",
  "M9 8h6", "M17 16h6"], "Sliders");
export const IconLock = make(["M5 11h14v10H5z", "M8 11V7a4 4 0 0 1 8 0v4"], "Lock");
export const IconLogout = make(["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "M16 17l5-5-5-5", "M21 12H9"], "Logout");
export const IconMenu = make(["M4 6h16", "M4 12h16", "M4 18h16"], "Menu");
export const IconClose = make(["M18 6L6 18", "M6 6l12 12"], "Close");
export const IconExternal = make(["M14 3h7v7", "M10 14L21 3", "M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"],
  "External");
export const IconDownload = make(["M12 3v12", "M7 10l5 5 5-5", "M5 21h14"], "Download");
export const IconStop = make(["M6 6h12v12H6z"], "Stop");
export const IconRefresh = make(["M21 12a9 9 0 1 1-3-6.7L21 8", "M21 3v5h-5"], "Refresh");
export const IconAlert = make(["M12 9v4", "M12 17h.01", "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"],
  "Alert");
export const IconCheck = make(["M20 6L9 17l-5-5"], "Check");
export const IconSearch = make(["M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z", "M21 21l-4.3-4.3"], "Search");
export const IconChevron = make(["M9 18l6-6-6-6"], "Chevron");
export const IconDoc = make(["M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z", "M14 3v6h6", "M8 13h8",
  "M8 17h5"], "Doc");
export const IconPlay = make(["M6 4l14 8-14 8z"], "Play");
export const IconServer = make(["M3 5h18v6H3z", "M3 13h18v6H3z", "M7 8h.01", "M7 16h.01"], "Server");
export const IconCopy = make(["M9 9h11v11H9z", "M5 15H4V4h11v1"], "Copy");

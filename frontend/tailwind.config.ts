import type { Config } from "tailwindcss";

// Every color is a design token defined once in src/styles/tokens.css (RGB channels, so Tailwind can apply alpha).
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: { DEFAULT: token("c-bg"), secondary: token("c-bg-secondary"), deep: token("c-bg-deep") },
        ink: { DEFAULT: token("c-text"), muted: token("c-muted"), faint: token("c-faint") },
        accent: { DEFAULT: token("c-accent"), soft: token("c-accent-soft") },
        violet: { DEFAULT: token("c-purple") },
        ok: token("c-ok"),
        warn: token("c-warn"),
        danger: token("c-danger"),
        info: token("c-info"),
      },
      backgroundColor: {
        glass: "var(--glass)",
        "glass-hover": "var(--glass-hover)",
        "glass-strong": "var(--glass-strong)",
      },
      borderColor: {
        line: "var(--line)",
        "line-strong": "var(--line-strong)",
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      borderRadius: {
        panel: "var(--radius-panel)",
        card: "var(--radius-card)",
      },
      boxShadow: {
        glow: "0 0 0 1px rgb(var(--c-accent) / 0.35), 0 0 28px -6px rgb(var(--c-accent) / 0.45)",
        panel: "0 24px 60px -30px rgb(0 0 0 / 0.65)",
      },
      backdropBlur: { glass: "var(--blur)" },
      keyframes: {
        "fade-in": { from: { opacity: "0", transform: "translateY(4px)" }, to: { opacity: "1", transform: "none" } },
        pulse_soft: { "0%,100%": { opacity: "0.55" }, "50%": { opacity: "1" } },
        shimmer: { from: { backgroundPosition: "-200% 0" }, to: { backgroundPosition: "200% 0" } },
      },
      animation: {
        "fade-in": "fade-in 220ms ease-out both",
        "pulse-soft": "pulse_soft 2.4s ease-in-out infinite",
        shimmer: "shimmer 1.8s linear infinite",
      },
    },
  },
  plugins: [],
} satisfies Config;

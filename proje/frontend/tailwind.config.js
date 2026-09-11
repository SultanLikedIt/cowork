// package.json declares "type": "module", so this file is ESM -- `require`
// is not defined here and using it fails the whole PostCSS pipeline (dev
// server and production build alike). The plugin is imported instead.
import animate from 'tailwindcss-animate'

/** @type {import('tailwindcss').Config} */
export default {
  // Class-based, not media-based: the app is dark-first (index.css puts
  // `dark` on <html>), and a media strategy would let the OS silently
  // override that. See src/index.css for the token definitions.
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Every colour is an HSL triple in a CSS variable so the light and
        // dark themes are a pure token swap -- no component ever names a
        // raw colour. `<alpha-value>` keeps Tailwind's /opacity modifiers
        // working (e.g. bg-accent/40).
        border: 'hsl(var(--border) / <alpha-value>)',
        input: 'hsl(var(--input) / <alpha-value>)',
        ring: 'hsl(var(--ring) / <alpha-value>)',
        background: 'hsl(var(--background) / <alpha-value>)',
        foreground: 'hsl(var(--foreground) / <alpha-value>)',
        primary: {
          DEFAULT: 'hsl(var(--primary) / <alpha-value>)',
          foreground: 'hsl(var(--primary-foreground) / <alpha-value>)',
        },
        // Second brand hue -- brand mark + ambient glow only, see
        // index.css's --accent-secondary comment. Not a semantic color.
        'accent-secondary': 'hsl(var(--accent-secondary) / <alpha-value>)',
        // Accent *text* on a dark ground. --primary is tuned so white sits
        // on it accessibly, which makes it too dark to read as text against
        // the near-black canvas; --link is the lighter counterpart used for
        // links, live labels and running badges. See index.css.
        link: 'hsl(var(--link) / <alpha-value>)',
        secondary: {
          DEFAULT: 'hsl(var(--secondary) / <alpha-value>)',
          foreground: 'hsl(var(--secondary-foreground) / <alpha-value>)',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted) / <alpha-value>)',
          foreground: 'hsl(var(--muted-foreground) / <alpha-value>)',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent) / <alpha-value>)',
          foreground: 'hsl(var(--accent-foreground) / <alpha-value>)',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive) / <alpha-value>)',
          foreground: 'hsl(var(--destructive-foreground) / <alpha-value>)',
        },
        success: {
          DEFAULT: 'hsl(var(--success) / <alpha-value>)',
          foreground: 'hsl(var(--success-foreground) / <alpha-value>)',
        },
        warning: {
          DEFAULT: 'hsl(var(--warning) / <alpha-value>)',
          foreground: 'hsl(var(--warning-foreground) / <alpha-value>)',
        },
        // Surfaces. `panel` is the sidebar/rail ground, `card` sits on top
        // of it. Keeping them distinct is what lets hierarchy come from
        // elevation instead of from borders everywhere.
        panel: {
          DEFAULT: 'hsl(var(--panel) / <alpha-value>)',
          foreground: 'hsl(var(--panel-foreground) / <alpha-value>)',
        },
        card: {
          DEFAULT: 'hsl(var(--card) / <alpha-value>)',
          foreground: 'hsl(var(--card-foreground) / <alpha-value>)',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover) / <alpha-value>)',
          foreground: 'hsl(var(--popover-foreground) / <alpha-value>)',
        },
      },
      borderRadius: {
        // One corner scale, derived from --radius (10px), so controls of
        // the same kind keep the same corner everywhere: sm=6 for chips and
        // inline marks, md=8 for buttons and list rows, lg=10 for cards and
        // panels, xl=14 for the composer and dialogs.
        sm: 'calc(var(--radius) - 4px)',
        md: 'calc(var(--radius) - 2px)',
        lg: 'var(--radius)',
        xl: 'calc(var(--radius) + 4px)',
      },
      fontFamily: {
        sans: [
          'Inter',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'Helvetica',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'SF Mono',
          'Menlo',
          'Consolas',
          'Liberation Mono',
          'monospace',
        ],
      },
      keyframes: {
        'accordion-down': {
          from: { height: '0' },
          to: { height: 'var(--radix-accordion-content-height)' },
        },
        'accordion-up': {
          from: { height: 'var(--radix-accordion-content-height)' },
          to: { height: '0' },
        },
        // Used by the live "working" indicators. Deliberately slow and
        // low-contrast: it signals activity without drawing the eye away
        // from the answer text.
        'pulse-soft': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.45' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
      },
      animation: {
        'accordion-down': 'accordion-down 160ms ease-out',
        'accordion-up': 'accordion-up 160ms ease-out',
        'pulse-soft': 'pulse-soft 1.8s ease-in-out infinite',
        shimmer: 'shimmer 1.6s infinite',
      },
    },
  },
  plugins: [animate],
}

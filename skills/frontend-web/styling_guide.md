# Styling Guide

CSS patterns for production React SPAs: Tailwind CSS, CSS Modules, design tokens, and layout.

## Choosing a Styling Approach

| Approach | Best for |
|---|---|
| **Tailwind CSS** | Fast iteration, consistent design system, utility-first teams |
| **CSS Modules** | Scoped styles without a framework; co-located with components |
| **CSS-in-JS** (Emotion, Stitches) | Dynamic styles driven heavily by JS state (avoid in most SPAs) |

**Recommendation**: Use **Tailwind CSS** for new projects. Use **CSS Modules** when Tailwind is not viable or when migrating an existing codebase. Avoid runtime CSS-in-JS — it increases bundle size and hurts SSR.

---

## Tailwind CSS

### Setup with Vite

```bash
npm install -D tailwindcss postcss autoprefixer
npx tailwindcss init -p
```

```ts
// tailwind.config.ts
import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  "#eff6ff",
          500: "#3b82f6",
          900: "#1e3a8a",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
} satisfies Config;
```

```css
/* src/index.css */
@tailwind base;
@tailwind components;
@tailwind utilities;
```

### Class organization

Order Tailwind classes consistently: layout → sizing → spacing → typography → color → effects → states → responsive.

Use `clsx` or `cn` (clsx + tailwind-merge) for conditional classes:

```tsx
import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: Parameters<typeof clsx>) {
  return twMerge(clsx(inputs));
}

// Usage
<button
  className={cn(
    "rounded px-4 py-2 font-medium transition-colors",
    variant === "primary" && "bg-brand-500 text-white hover:bg-brand-600",
    variant === "ghost"   && "text-brand-500 hover:bg-brand-50",
    disabled && "opacity-50 cursor-not-allowed",
  )}
>
```

### Extract repeated patterns into components, not `@apply`

Avoid `@apply` — it defeats the purpose of utility classes and hurts purging. If a pattern repeats, extract it as a React component:

```tsx
// BAD — using @apply
/* button.css */
.btn-primary { @apply bg-brand-500 text-white rounded px-4 py-2; }

// GOOD — component encapsulates the classes
export function PrimaryButton(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button {...props} className={cn("bg-brand-500 text-white rounded px-4 py-2", props.className)} />;
}
```

---

## CSS Modules

### File naming and co-location

Place the CSS Module file next to the component:

```
src/components/Card/
├── Card.tsx
├── Card.module.css
└── index.ts          (re-export)
```

### Naming conventions

Use camelCase for class names in `.module.css` so they work as property access:

```css
/* Card.module.css */
.card        { border-radius: 0.5rem; box-shadow: var(--shadow-md); }
.cardTitle   { font-size: 1.125rem; font-weight: 600; }
.cardBody    { padding: 1rem; }
.cardVariant { /* variant-specific overrides */ }
```

```tsx
import styles from "./Card.module.css";

export function Card({ title, children }: CardProps) {
  return (
    <div className={styles.card}>
      <h2 className={styles.cardTitle}>{title}</h2>
      <div className={styles.cardBody}>{children}</div>
    </div>
  );
}
```

---

## Design Tokens

Define tokens as CSS custom properties in a single file — this is the single source of truth:

```css
/* src/styles/tokens.css */
:root {
  /* Color */
  --color-brand-50:   #eff6ff;
  --color-brand-500:  #3b82f6;
  --color-brand-900:  #1e3a8a;
  --color-gray-50:    #f9fafb;
  --color-gray-900:   #111827;
  --color-error:      #ef4444;
  --color-success:    #22c55e;

  /* Spacing (4-point grid) */
  --space-1: 0.25rem;
  --space-2: 0.5rem;
  --space-4: 1rem;
  --space-6: 1.5rem;
  --space-8: 2rem;

  /* Typography */
  --font-sans: "Inter", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", monospace;
  --text-sm:   0.875rem;
  --text-base: 1rem;
  --text-lg:   1.125rem;
  --text-xl:   1.25rem;
  --text-2xl:  1.5rem;

  /* Shadows */
  --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
  --shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1);

  /* Border radius */
  --radius-sm: 0.25rem;
  --radius-md: 0.375rem;
  --radius-lg: 0.5rem;
  --radius-full: 9999px;

  /* Transitions */
  --transition-fast:   150ms ease;
  --transition-normal: 250ms ease;
}
```

### Dark mode

Use the `class` strategy (not `media`) so users can override the system preference:

```css
/* tokens.css */
:root        { --bg: var(--color-gray-50); --fg: var(--color-gray-900); }
.dark :root  { --bg: var(--color-gray-900); --fg: var(--color-gray-50); }
```

```tsx
// Toggle by adding/removing "dark" class on <html>
function toggleDark() {
  document.documentElement.classList.toggle("dark");
}
```

---

## Layout

### Flexbox patterns

```css
/* Horizontal center */
.row { display: flex; align-items: center; gap: var(--space-4); }

/* Grow a child to fill remaining space */
.row .spacer { flex: 1; }

/* Vertical stack */
.stack { display: flex; flex-direction: column; gap: var(--space-4); }
```

### CSS Grid for page layouts

```css
.appLayout {
  display: grid;
  grid-template-columns: 240px 1fr;
  grid-template-rows: 56px 1fr;
  min-height: 100dvh;
}

.sidebar { grid-row: 1 / -1; }
.header  { grid-column: 2; }
.main    { grid-column: 2; overflow-y: auto; padding: var(--space-6); }
```

### Responsive breakpoints

Use mobile-first — write base styles for mobile, then override for larger screens:

```css
.grid {
  display: grid;
  grid-template-columns: 1fr;          /* mobile: 1 column */
  gap: var(--space-4);
}

@media (min-width: 768px) {
  .grid { grid-template-columns: repeat(2, 1fr); }   /* tablet: 2 cols */
}

@media (min-width: 1280px) {
  .grid { grid-template-columns: repeat(3, 1fr); }   /* desktop: 3 cols */
}
```

Standard breakpoints: `480px` (xs), `768px` (md), `1024px` (lg), `1280px` (xl).

---

## Typography

```css
/* Global base */
body {
  font-family: var(--font-sans);
  font-size: var(--text-base);
  line-height: 1.6;
  color: var(--fg);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
}

/* Heading scale */
h1 { font-size: var(--text-2xl); font-weight: 700; line-height: 1.2; }
h2 { font-size: var(--text-xl);  font-weight: 600; line-height: 1.3; }
h3 { font-size: var(--text-lg);  font-weight: 600; }
```

---

## Common Anti-Patterns

| Anti-pattern | Fix |
|---|---|
| Inline `style={{ color: "red" }}` for theming | Use CSS variables or Tailwind classes |
| `!important` in component styles | Fix specificity — use a wrapper class or CSS Modules |
| Hard-coded pixel values | Use token variables (`var(--space-4)`) |
| CSS class name collisions in global styles | Use CSS Modules or BEM naming |
| `position: absolute` for layout | Use flexbox/grid; reserve absolute for overlays |
| Animating `width`/`height` | Animate `transform`/`opacity` — compositor-only, no layout thrash |
| Forgetting `prefers-reduced-motion` | Wrap animations: `@media (prefers-reduced-motion: no-preference) { }` |

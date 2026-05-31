---
name: frontend-web
description: Best practices for modern web SPAs with React, TypeScript, Vite, styling, and routing.
---

# Frontend Web Development

Best practices for building production-quality single-page applications with React, TypeScript,
Vite, and modern CSS patterns.

## Reference Documents

- `react_patterns.md` — Component design, hooks, performance, and accessibility
- `typescript_guide.md` — TypeScript for SPAs: strict config, utility types, generics, and API contracts
- `build_and_bundling.md` — Vite configuration, code splitting, tree shaking, and environment variables
- `styling_guide.md` — CSS Modules, Tailwind CSS, design tokens, and responsive layout
- `routing_and_state.md` — React Router v6, TanStack Router, Zustand, and data-fetching patterns

---

## Overview

Use this skill when building or reviewing React SPAs. It covers the full stack from
project setup through production deployment, focusing on correctness, performance, and maintainability.

### When to use `react_patterns.md`

- Designing component APIs and deciding component boundaries
- Implementing custom hooks, context, or refs
- Optimizing renders with `memo`, `useMemo`, or `useCallback`
- Handling side effects, async data, or form state
- Meeting WCAG accessibility requirements

### When to use `typescript_guide.md`

- Configuring `tsconfig.json` for strict mode
- Typing component props, events, and refs
- Writing generic utility types or discriminated unions
- Typing API responses and async data flows
- Avoiding `any` and unsafe casts

### When to use `build_and_bundling.md`

- Setting up or modifying `vite.config.ts`
- Configuring code splitting and lazy routes
- Managing environment variables across dev/staging/prod
- Optimizing bundle size and analyzing chunks
- Setting up path aliases and monorepo workspaces

### When to use `styling_guide.md`

- Choosing between CSS Modules, Tailwind, or CSS-in-JS
- Implementing design tokens (colors, spacing, typography)
- Building responsive layouts with flexbox/grid
- Writing CSS that survives refactors (naming, specificity)
- Theming and dark mode

### When to use `routing_and_state.md`

- Setting up React Router v6 or TanStack Router
- Structuring nested routes and layouts
- Managing global state with Zustand or Jotai
- Data fetching with TanStack Query (React Query)
- Handling loading, error, and empty states across pages

### General Principles

- **TypeScript strict mode always** — `"strict": true` in tsconfig; never disable individual checks to silence errors.
- **Component = pure function** — A component describes UI given props; all side effects go in hooks.
- **Colocation** — Keep styles, tests, and types next to the component they belong to.
- **Code-split at route boundaries** — Lazy-load every route; never import page components eagerly from the router entry.
- **Measure before optimizing** — Profile with React DevTools before adding `memo` or `useMemo`.
- **Accessibility is not optional** — Every interactive element must be keyboard-navigable and have a label.

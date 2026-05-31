# Build & Bundling

Production-quality Vite configuration for React + TypeScript SPAs.

## Project Setup

Scaffold a new project:

```bash
npm create vite@latest my-app -- --template react-ts
cd my-app
npm install
```

Recommended additional packages:

```bash
# TypeScript path aliases
npm install -D vite-tsconfig-paths

# Bundle analysis
npm install -D rollup-plugin-visualizer
```

## vite.config.ts

```ts
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";
import { visualizer } from "rollup-plugin-visualizer";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  return {
    plugins: [
      react(),
      tsconfigPaths(),
      // Only include visualizer in analyze mode: ANALYZE=true npm run build
      env.ANALYZE === "true" && visualizer({ open: true, gzipSize: true }),
    ].filter(Boolean),

    build: {
      target: "es2022",
      sourcemap: true,
      rollupOptions: {
        output: {
          // Manual chunk splitting — vendor libs in a stable long-cached chunk
          manualChunks: {
            react: ["react", "react-dom"],
            router: ["react-router-dom"],
            query: ["@tanstack/react-query"],
          },
        },
      },
    },

    server: {
      port: 3000,
      proxy: {
        "/api": {
          target: env.VITE_API_URL ?? "http://localhost:8000",
          changeOrigin: true,
        },
      },
    },
  };
});
```

## Code Splitting

### Lazy routes (always)

Every top-level route should be lazy-loaded. Never eagerly import page components:

```tsx
import { lazy, Suspense } from "react";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const Settings  = lazy(() => import("./pages/Settings"));
const Reports   = lazy(() => import("./pages/Reports"));

const router = createBrowserRouter([
  {
    path: "/",
    element: <RootLayout />,
    children: [
      { index: true,       element: <Suspense fallback={<PageSpinner />}><Dashboard /></Suspense> },
      { path: "settings",  element: <Suspense fallback={<PageSpinner />}><Settings /></Suspense> },
      { path: "reports",   element: <Suspense fallback={<PageSpinner />}><Reports /></Suspense> },
    ],
  },
]);
```

### Dynamic imports for heavy features

```tsx
async function openEditor() {
  const { MonacoEditor } = await import("@/components/MonacoEditor");
  // render it
}
```

Import heavy libraries (charts, editors, maps) only when the user triggers the feature.

## Environment Variables

Vite exposes only variables prefixed with `VITE_` to client code.

```
# .env                 loaded always
# .env.local           loaded always, gitignored
# .env.development     loaded in dev mode
# .env.production      loaded in prod build
VITE_API_URL=http://localhost:8000
VITE_SENTRY_DSN=
VITE_APP_VERSION=$npm_package_version
```

Access in code:

```ts
const apiUrl = import.meta.env.VITE_API_URL;
```

Type the env module to prevent typos:

```ts
// src/vite-env.d.ts  (auto-generated, extend it)
interface ImportMetaEnv {
  readonly VITE_API_URL: string;
  readonly VITE_SENTRY_DSN: string;
}
```

Never put secrets (API keys, tokens) in `VITE_*` variables — they are bundled into the JS served to browsers.

## Bundle Size Rules

- **Keep initial JS under 200 kB gzipped** for fast first load on mobile.
- Run `ANALYZE=true npm run build` to open the treemap after adding dependencies.
- Prefer named imports over namespace imports to enable tree-shaking:

```ts
// GOOD — tree-shakeable
import { format, parseISO } from "date-fns";

// BAD — imports entire library
import * as dateFns from "date-fns";
```

- Check `bundlephobia.com` before adding a new dependency. Prefer zero-dependency or small alternatives (e.g., `date-fns` over `moment`, `zustand` over `redux`).

## Asset Handling

Vite handles static assets automatically:

```ts
// Import as URL (hashed filename, cache-forever)
import logoUrl from "./assets/logo.svg";
<img src={logoUrl} alt="Logo" />

// Import SVG as React component
import { ReactComponent as Logo } from "./assets/logo.svg?react";
```

Place large binaries (PDFs, videos) in `public/` — they are copied as-is without hashing.

## TypeScript Build Check

Run `tsc --noEmit` as a separate CI step — Vite does not type-check during build:

```json
// package.json scripts
{
  "type-check": "tsc --noEmit",
  "build": "npm run type-check && vite build",
  "lint": "eslint src --ext .ts,.tsx --max-warnings 0"
}
```

## Linting & Formatting

```bash
npm install -D eslint @eslint/js eslint-plugin-react-hooks eslint-plugin-jsx-a11y
npm install -D prettier eslint-config-prettier
```

Minimal `eslint.config.js` (flat config):

```js
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";

export default [
  js.configs.recommended,
  { plugins: { "react-hooks": reactHooks }, rules: reactHooks.configs.recommended.rules },
  { plugins: { "jsx-a11y": jsxA11y }, rules: jsxA11y.configs.recommended.rules },
  { rules: { "no-console": "warn" } },
];
```

`.prettierrc`:

```json
{
  "semi": true,
  "singleQuote": false,
  "trailingComma": "all",
  "printWidth": 100
}
```

## CI Pipeline

```yaml
# .github/workflows/ci.yml
jobs:
  quality:
    steps:
      - run: npm ci
      - run: npm run type-check
      - run: npm run lint
      - run: npm test -- --run
      - run: npm run build
```

## Common Pitfalls

| Problem | Fix |
|---|---|
| Large first bundle | Lazy-load routes; check `manualChunks` |
| `__dirname` not defined | Use `import.meta.url` and `fileURLToPath` |
| Env var `undefined` at runtime | Ensure it starts with `VITE_` and `.env` is loaded |
| Types from `@types/*` missing | Check `skipLibCheck` and `include` in tsconfig |
| Hot reload breaks on context change | Wrap Provider in a stable module boundary |
| Build passes but types fail in IDE | Run `tsc --noEmit` in CI; Vite skips type checks |

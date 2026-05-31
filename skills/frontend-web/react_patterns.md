# React Patterns

Production-quality conventions for React 18+ with TypeScript.

## Component Design

### Prefer function components with explicit prop types

```tsx
interface ButtonProps {
  label: string;
  onClick: () => void;
  variant?: "primary" | "secondary" | "ghost";
  disabled?: boolean;
}

export function Button({ label, onClick, variant = "primary", disabled = false }: ButtonProps) {
  return (
    <button
      className={styles[variant]}
      onClick={onClick}
      disabled={disabled}
      type="button"
    >
      {label}
    </button>
  );
}
```

- Export named, not default — easier to refactor and find in search.
- Destructure props in the signature, not inside the body.
- Give optional props explicit defaults in the destructure, not inside the function body.
- `type="button"` on every `<button>` that is not a form submit — prevents accidental form submission.

### Component boundaries

Split a component when:
1. It has more than one reason to change (SRP).
2. A subtree needs independent re-render isolation.
3. The same JSX appears in two places.

Keep together when splitting would require more prop-drilling than it saves.

### Compound components

For related UI with shared implicit state (Tabs, Accordion, Select), use a context-based compound pattern:

```tsx
const TabsContext = React.createContext<TabsContextValue | null>(null);

function useTabs() {
  const ctx = React.useContext(TabsContext);
  if (!ctx) throw new Error("useTabs must be used inside <Tabs>");
  return ctx;
}

export function Tabs({ children, defaultTab }: TabsProps) {
  const [active, setActive] = React.useState(defaultTab);
  return (
    <TabsContext.Provider value={{ active, setActive }}>
      <div role="tablist">{children}</div>
    </TabsContext.Provider>
  );
}

export function Tab({ id, label }: TabProps) {
  const { active, setActive } = useTabs();
  return (
    <button role="tab" aria-selected={active === id} onClick={() => setActive(id)}>
      {label}
    </button>
  );
}
```

## Hooks

### Custom hooks encapsulate logic, not UI

A custom hook returns values and callbacks — never JSX.

```tsx
function useLocalStorage<T>(key: string, initial: T) {
  const [value, setValue] = React.useState<T>(() => {
    try {
      const stored = localStorage.getItem(key);
      return stored ? (JSON.parse(stored) as T) : initial;
    } catch {
      return initial;
    }
  });

  const set = React.useCallback(
    (next: T) => {
      setValue(next);
      localStorage.setItem(key, JSON.stringify(next));
    },
    [key],
  );

  return [value, set] as const;
}
```

### `useEffect` rules

- One concern per `useEffect`. If two effects share a cleanup, they should share a hook.
- Always return a cleanup function for subscriptions, timers, and event listeners.
- Prefer derived state and event handlers over `useEffect` when possible.
- Never set state unconditionally inside `useEffect` without a dependency-driven guard — causes render loops.

```tsx
// BAD: triggers on every render
useEffect(() => {
  setFormatted(format(value));
});

// GOOD: derived value, no effect needed
const formatted = format(value);
```

### `useRef` patterns

Use `ref` for:
- DOM element access (focus, measure, scroll)
- Mutable values that must not trigger re-renders (timers, previous values, cancel tokens)

```tsx
function SearchInput({ onSearch }: { onSearch: (q: string) => void }) {
  const inputRef = React.useRef<HTMLInputElement>(null);
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  function handleChange(e: React.ChangeEvent<HTMLInputElement>) {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => onSearch(e.target.value), 300);
  }

  return <input ref={inputRef} onChange={handleChange} />;
}
```

## Performance

### Measure first

Use React DevTools Profiler before adding memoization. The cost of `useMemo`/`useCallback`
(closure allocation, dependency comparison) is often higher than a cheap re-render.

### `React.memo`

Wrap a component with `memo` only when:
- It receives the same props on most renders, AND
- It renders expensively (large list item, canvas, chart)

```tsx
const Row = React.memo(function Row({ item, onSelect }: RowProps) {
  return <div onClick={() => onSelect(item.id)}>{item.name}</div>;
});
```

### `useMemo` and `useCallback`

```tsx
// useMemo: memoize expensive derived data
const sorted = React.useMemo(
  () => items.slice().sort((a, b) => a.name.localeCompare(b.name)),
  [items],
);

// useCallback: stable function reference passed to memo'd children
const handleSelect = React.useCallback((id: string) => {
  dispatch({ type: "SELECT", id });
}, [dispatch]);
```

### Virtualization

For lists over ~100 items, use `@tanstack/react-virtual` rather than rendering all rows:

```tsx
import { useVirtualizer } from "@tanstack/react-virtual";

const rowVirtualizer = useVirtualizer({
  count: rows.length,
  getScrollElement: () => parentRef.current,
  estimateSize: () => 40,
});
```

## State Management

### Local → lifted → context → external store

1. **Local state** — `useState` inside the component.
2. **Lifted state** — move up to the nearest common ancestor.
3. **Context** — for infrequently-changing values (theme, locale, auth user).
4. **External store** (Zustand/Jotai) — for frequently-changing shared state.

Never use context for high-frequency updates (mouse position, form fields) — it re-renders all consumers.

### Forms

Prefer [React Hook Form](https://react-hook-form.com/) for complex forms; plain `useState` for 1-3 fields.

```tsx
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

const schema = z.object({
  email: z.string().email(),
  password: z.string().min(8),
});
type FormValues = z.infer<typeof schema>;

function LoginForm({ onSubmit }: { onSubmit: (v: FormValues) => Promise<void> }) {
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<FormValues>({
    resolver: zodResolver(schema),
  });

  return (
    <form onSubmit={handleSubmit(onSubmit)}>
      <input {...register("email")} aria-describedby="email-error" />
      {errors.email && <span id="email-error" role="alert">{errors.email.message}</span>}
      <button type="submit" disabled={isSubmitting}>Login</button>
    </form>
  );
}
```

## Accessibility

- Every `<img>` has `alt`. Decorative images: `alt=""`.
- Every form input has a visible `<label>` associated via `htmlFor` / `id`.
- Every icon-only button has `aria-label` or `aria-labelledby`.
- Focus is managed after modals open/close — use `focus()` on the first interactive element.
- Color alone never conveys information — pair with text or icon.
- Test with keyboard only and with a screen reader (VoiceOver / NVDA).

```tsx
// BAD
<button onClick={close}><XIcon /></button>

// GOOD
<button onClick={close} aria-label="Close dialog"><XIcon aria-hidden /></button>
```

## Error Boundaries

Wrap async/external sections in an error boundary to prevent full-page crashes:

```tsx
import { ErrorBoundary } from "react-error-boundary";

<ErrorBoundary fallback={<p>Something went wrong. <button onClick={resetErrorBoundary}>Retry</button></p>}>
  <DataGrid />
</ErrorBoundary>
```

Use `react-error-boundary` rather than rolling a class component by hand.

## Common Anti-Patterns

| Anti-pattern | Fix |
|---|---|
| Index as list `key` when list reorders | Use stable unique IDs |
| State derived from props (`useState(prop)`) | Derive at render or use `key` to reset |
| `useEffect` for data fetching | Use TanStack Query or a loader |
| Inline object/array props `<C style={{ color: "red" }}>` | Extract constant or memoize |
| `any` in event handlers | Use `React.ChangeEvent<HTMLInputElement>` etc. |
| Deep prop drilling 3+ levels | Lift to context or store |
| `document.getElementById` in React | Use `useRef` |

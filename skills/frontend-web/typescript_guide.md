# TypeScript for Frontend SPAs

TypeScript conventions for React 18+ projects targeting strict type safety.

## tsconfig.json

Start with strict mode and target `ES2022`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "skipLibCheck": true,
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["src"],
  "exclude": ["node_modules", "dist"]
}
```

Key non-obvious flags:
- `"moduleResolution": "Bundler"` — matches what Vite/esbuild actually resolve; required for `.ts` extension imports and package exports.
- `"noUncheckedIndexedAccess": true` — `arr[0]` has type `T | undefined`; forces safe array access.
- `"exactOptionalPropertyTypes": true` — `{ foo?: string }` means the key can be absent OR `string`, not `string | undefined`.

Never silence the compiler with `// @ts-ignore`. Use `// @ts-expect-error` with a comment when suppression is truly necessary (e.g., testing an error path).

## Typing Component Props

```tsx
// Extend HTML element props to pass through aria-*, data-*, event handlers
interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  title: string;
  footer?: React.ReactNode;
}

export function Card({ title, footer, children, ...rest }: CardProps) {
  return (
    <div {...rest}>
      <h2>{title}</h2>
      {children}
      {footer}
    </div>
  );
}
```

Use `React.ReactNode` for children / slots — it accepts `string`, `JSX.Element`, `null`, arrays.
Use `React.ReactElement` only when you need to clone or inspect the element.

## Discriminated Unions

Model states explicitly so the compiler enforces you handle all cases:

```ts
type AsyncState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; data: T }
  | { status: "error"; error: Error };

function render<T>(state: AsyncState<T>) {
  switch (state.status) {
    case "idle":    return <Placeholder />;
    case "loading": return <Spinner />;
    case "success": return <DataView data={state.data} />;
    case "error":   return <ErrorMessage error={state.error} />;
    // TypeScript errors here if a case is missing (exhaustive check)
  }
}
```

## Utility Types

| Utility | Use case |
|---------|----------|
| `Partial<T>` | All props optional (update payloads) |
| `Required<T>` | All props required |
| `Readonly<T>` | Immutable snapshot |
| `Pick<T, K>` | Subset of keys |
| `Omit<T, K>` | All keys except K |
| `Record<K, V>` | Dictionary with known key type |
| `ReturnType<F>` | Infer function return type |
| `Parameters<F>` | Infer function parameter tuple |
| `NonNullable<T>` | Remove `null | undefined` |
| `Awaited<T>` | Unwrap promise type |

```ts
// Derive form state type from API response type
type UserFormValues = Pick<User, "name" | "email" | "role">;

// Narrow a prop to exclude null
function assertDefined<T>(v: T | null | undefined, msg: string): T {
  if (v == null) throw new Error(msg);
  return v;
}
```

## Typing Event Handlers

```tsx
// Form events
function handleChange(e: React.ChangeEvent<HTMLInputElement>) { }
function handleSubmit(e: React.FormEvent<HTMLFormElement>) { e.preventDefault(); }

// Keyboard / mouse
function handleKeyDown(e: React.KeyboardEvent<HTMLButtonElement>) { }
function handleClick(e: React.MouseEvent<HTMLAnchorElement>) { }

// Generic handler type
type ChangeHandler = React.ChangeEventHandler<HTMLInputElement>;
```

## Generics in Components

```tsx
interface SelectProps<T> {
  options: T[];
  value: T | null;
  getLabel: (option: T) => string;
  getValue: (option: T) => string;
  onChange: (option: T | null) => void;
}

export function Select<T>({ options, value, getLabel, getValue, onChange }: SelectProps<T>) {
  return (
    <select
      value={value ? getValue(value) : ""}
      onChange={(e) => {
        const found = options.find((o) => getValue(o) === e.target.value) ?? null;
        onChange(found);
      }}
    >
      {options.map((o) => (
        <option key={getValue(o)} value={getValue(o)}>{getLabel(o)}</option>
      ))}
    </select>
  );
}
```

## API Contracts

Use `zod` to parse and type-narrow API responses at the boundary:

```ts
import { z } from "zod";

const UserSchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  role: z.enum(["admin", "editor", "viewer"]),
  createdAt: z.string().datetime(),
});

type User = z.infer<typeof UserSchema>;

async function fetchUser(id: string): Promise<User> {
  const res = await fetch(`/api/users/${id}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return UserSchema.parse(await res.json());
}
```

This guarantees the returned `User` type matches reality, not just the expected shape.

## Typing Refs

```tsx
// DOM element
const inputRef = React.useRef<HTMLInputElement>(null);

// Mutable value (no null — initialized immediately)
const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

// Forward ref
const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ label, ...props }, ref) => <input ref={ref} aria-label={label} {...props} />,
);
```

## Path Aliases

With `"paths": { "@/*": ["./src/*"] }` in tsconfig and a matching alias in `vite.config.ts`:

```ts
// vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";

export default defineConfig({
  plugins: [react(), tsconfigPaths()],
});
```

Import as:
```ts
import { Button } from "@/components/Button";
import type { User } from "@/types/api";
```

Never use relative imports that traverse more than one level up (`../../..`).

## Common Anti-Patterns

| Anti-pattern | Fix |
|---|---|
| `as unknown as T` | Parse with zod or narrow properly |
| `any` in props | Use `unknown` and narrow, or find the right type |
| Optional chaining everywhere `a?.b?.c?.d` | Model nullability explicitly; most values shouldn't be optional |
| `interface` for union types | Use `type`; `interface` is for object shapes that may be extended |
| Disabling strict checks in tsconfig | Fix the underlying types instead |
| `React.FC<Props>` | Just write `function Foo(props: Props)` — FC hides the return type |

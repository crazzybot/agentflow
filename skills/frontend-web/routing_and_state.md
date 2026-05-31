# Routing & State Management

React Router v6, TanStack Router, Zustand global state, and TanStack Query data fetching.

---

## React Router v6

### Recommended structure

```tsx
// src/main.tsx
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { lazy, Suspense } from "react";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const UserList  = lazy(() => import("./pages/UserList"));
const UserDetail = lazy(() => import("./pages/UserDetail"));
const NotFound  = lazy(() => import("./pages/NotFound"));

const router = createBrowserRouter([
  {
    path: "/",
    element: <RootLayout />,
    errorElement: <RootError />,
    children: [
      { index: true, element: <Suspense fallback={<Spinner />}><Dashboard /></Suspense> },
      {
        path: "users",
        children: [
          { index: true,    element: <Suspense fallback={<Spinner />}><UserList /></Suspense> },
          { path: ":userId", element: <Suspense fallback={<Spinner />}><UserDetail /></Suspense> },
        ],
      },
      { path: "*", element: <Suspense fallback={<Spinner />}><NotFound /></Suspense> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
```

### Layouts and Outlet

```tsx
// RootLayout.tsx — shell with nav + content area
export function RootLayout() {
  return (
    <div className={styles.appLayout}>
      <Sidebar />
      <Header />
      <main className={styles.main}>
        <Outlet />       {/* child route renders here */}
      </main>
    </div>
  );
}
```

Nest `<Outlet />` as deep as needed for nested layouts (e.g., a settings section with its own sidebar).

### Navigation

```tsx
import { Link, NavLink, useNavigate, useParams, useSearchParams } from "react-router-dom";

// Declarative links
<Link to="/users">Users</Link>
<NavLink to="/dashboard" className={({ isActive }) => isActive ? styles.active : ""}>
  Dashboard
</NavLink>

// Programmatic navigation
const navigate = useNavigate();
navigate("/users", { replace: true });   // replace history entry (e.g. after form submit)
navigate(-1);                            // go back

// Route params
const { userId } = useParams<{ userId: string }>();

// Query string
const [searchParams, setSearchParams] = useSearchParams();
const q = searchParams.get("q") ?? "";
setSearchParams({ q: newValue, page: "1" });
```

### Route-level data loading (loaders)

Use loaders to fetch data before the component renders — avoids loading spinners inside the page:

```tsx
// pages/UserDetail.tsx
export async function loader({ params }: LoaderFunctionArgs) {
  const user = await fetchUser(params.userId!);
  return { user };
}

export default function UserDetail() {
  const { user } = useLoaderData() as Awaited<ReturnType<typeof loader>>;
  return <h1>{user.name}</h1>;
}

// router definition
{ path: ":userId", element: <UserDetail />, loader: UserDetailModule.loader }
```

---

## TanStack Router (alternative to React Router)

Use TanStack Router when you need **type-safe routes** where route params and search params are fully typed end-to-end.

### Setup

```bash
npm install @tanstack/react-router
npm install -D @tanstack/router-devtools @tanstack/router-plugin
```

### File-based routing (recommended)

```
src/routes/
├── __root.tsx           # root layout
├── index.tsx            # /
├── users/
│   ├── index.tsx        # /users
│   └── $userId.tsx      # /users/:userId
└── settings.tsx         # /settings
```

```tsx
// src/routes/__root.tsx
import { createRootRoute, Outlet } from "@tanstack/react-router";

export const Route = createRootRoute({
  component: () => (
    <>
      <NavBar />
      <Outlet />
    </>
  ),
});

// src/routes/users/$userId.tsx
import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const searchSchema = z.object({ tab: z.enum(["profile", "activity"]).default("profile") });

export const Route = createFileRoute("/users/$userId")({
  validateSearch: searchSchema,
  loader: ({ params }) => fetchUser(params.userId),
  component: UserDetail,
});

function UserDetail() {
  const user = Route.useLoaderData();
  const { tab } = Route.useSearch();
  const { userId } = Route.useParams();  // fully typed string
  // ...
}
```

---

## Global State with Zustand

Use Zustand for shared mutable state that is accessed by unrelated parts of the tree.
Do NOT use Zustand for server data — use TanStack Query for that.

### Store definition

```ts
// src/store/ui.ts
import { create } from "zustand";
import { immer } from "zustand/middleware/immer";

interface UIState {
  sidebarOpen: boolean;
  notifications: Notification[];
  toggleSidebar: () => void;
  addNotification: (n: Omit<Notification, "id">) => void;
  dismissNotification: (id: string) => void;
}

export const useUIStore = create<UIState>()(
  immer((set) => ({
    sidebarOpen: true,
    notifications: [],
    toggleSidebar: () => set((s) => { s.sidebarOpen = !s.sidebarOpen; }),
    addNotification: (n) => set((s) => {
      s.notifications.push({ ...n, id: crypto.randomUUID() });
    }),
    dismissNotification: (id) => set((s) => {
      s.notifications = s.notifications.filter((n) => n.id !== id);
    }),
  })),
);
```

### Selectors — subscribe to only what you need

```tsx
// Re-renders ONLY when sidebarOpen changes, not on every store update
const sidebarOpen = useUIStore((s) => s.sidebarOpen);
const toggleSidebar = useUIStore((s) => s.toggleSidebar);
```

### Persisting to localStorage

```ts
import { persist, createJSONStorage } from "zustand/middleware";

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({ theme: "system", language: "en", setTheme: (t) => set({ theme: t }) }),
    { name: "app-settings", storage: createJSONStorage(() => localStorage) },
  ),
);
```

---

## Data Fetching with TanStack Query

TanStack Query manages server state: caching, background refresh, deduplication, loading/error states.

### Setup

```tsx
// src/main.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60,        // data is fresh for 1 minute
      gcTime:    1000 * 60 * 10,   // keep unused data in cache for 10 minutes
      retry: 1,
    },
  },
});

<QueryClientProvider client={queryClient}>
  <App />
</QueryClientProvider>
```

### Query keys

Use arrays as keys — they are serialized for caching and invalidation:

```ts
// src/queries/users.ts
export const userKeys = {
  all:    ["users"]                            as const,
  list:   (filters: UserFilters) => [...userKeys.all, "list", filters] as const,
  detail: (id: string)          => [...userKeys.all, "detail", id]     as const,
};
```

### Queries (read)

```tsx
import { useQuery } from "@tanstack/react-query";

function UserDetail({ userId }: { userId: string }) {
  const { data: user, isLoading, error } = useQuery({
    queryKey: userKeys.detail(userId),
    queryFn: () => fetchUser(userId),
  });

  if (isLoading) return <Spinner />;
  if (error)     return <ErrorMessage error={error} />;
  return <h1>{user.name}</h1>;
}
```

### Mutations (write)

```tsx
import { useMutation, useQueryClient } from "@tanstack/react-query";

function useUpdateUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (payload: UpdateUserPayload) => updateUser(payload),
    onSuccess: (updatedUser) => {
      // Update specific item in cache without refetch
      queryClient.setQueryData(userKeys.detail(updatedUser.id), updatedUser);
      // Invalidate list so it refetches on next view
      queryClient.invalidateQueries({ queryKey: userKeys.list({}) });
    },
  });
}

function EditUserForm({ userId }: { userId: string }) {
  const { mutate, isPending } = useUpdateUser();
  return (
    <form onSubmit={(e) => { e.preventDefault(); mutate({ id: userId, name: "..." }); }}>
      <button type="submit" disabled={isPending}>Save</button>
    </form>
  );
}
```

### Optimistic updates

```tsx
useMutation({
  mutationFn: toggleLike,
  onMutate: async (postId) => {
    await queryClient.cancelQueries({ queryKey: postKeys.detail(postId) });
    const previous = queryClient.getQueryData(postKeys.detail(postId));
    queryClient.setQueryData(postKeys.detail(postId), (old: Post) => ({
      ...old, liked: !old.liked, likes: old.liked ? old.likes - 1 : old.likes + 1,
    }));
    return { previous };
  },
  onError: (_err, postId, ctx) => {
    queryClient.setQueryData(postKeys.detail(postId), ctx?.previous);
  },
});
```

---

## Loading, Error, and Empty States

Every async view needs three non-happy states handled:

```tsx
function DataView() {
  const { data, isLoading, error } = useQuery({ queryKey: ["items"], queryFn: fetchItems });

  if (isLoading) return <SkeletonList count={5} />;    // show skeleton, not spinner
  if (error)     return <ErrorState error={error} onRetry={() => queryClient.invalidateQueries()} />;
  if (!data?.length) return <EmptyState message="No items yet." action={<CreateButton />} />;

  return <ItemGrid items={data} />;
}
```

Use **skeletons** over spinners for content that has a known shape — they reduce layout shift.

---

## Common Anti-Patterns

| Anti-pattern | Fix |
|---|---|
| Fetching in `useEffect` | Use TanStack Query `useQuery` |
| Global state for server data | Use TanStack Query cache |
| Store everything in one Zustand slice | Split stores by domain (ui, auth, cart) |
| `useNavigate` in a form `onSubmit` before awaiting mutation | Await the mutation, then navigate |
| `useParams` returning `undefined` | Ensure the route definition includes the param |
| Deeply nested routes with shared state | Lift state to a layout component via context |
| No error boundary around data-driven pages | Wrap each route in `<ErrorBoundary>` |

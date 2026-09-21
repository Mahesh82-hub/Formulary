# Dr. Insilico Web

The owned chat interface for Dr. Insilico. It provides passwordless sign-in, conversation
management, model selection, streamed assistant turns, tool activity, message editing, and
response regeneration across desktop and mobile layouts. It also includes an authenticated
bioequivalence workspace for exploratory comparisons, supplied-confidence-interval assessment,
concentration-time plots, and controlled evidence-package export.

## Development

```bash
cp .env.local.example .env.local
pnpm install
pnpm dev
```

The default API URL is `http://localhost:8000`. Override it with
`NEXT_PUBLIC_API_BASE_URL` when the API is hosted elsewhere.

## Checks

```bash
pnpm lint
pnpm typecheck
pnpm build
```

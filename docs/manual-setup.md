# Running it by hand

[← README](../README.md)

Useful when you want the two halves in separate terminals, or a production-ish
frontend build.

**Backend** (terminal 1):

```bash
cd image-to-ppt
python3 -m venv .venv                       # first time only
.venv/bin/python -m pip install -r backend/requirements.txt
export LITELLM_API_KEY='<your proxy key>'   # skip for /api/preview + /api/build-from-json
cd backend
../.venv/bin/python -m uvicorn app.main:app --port 8000 --reload
```

Check it:

```bash
curl -s http://127.0.0.1:8000/api/health
```

FastAPI also serves interactive docs at <http://127.0.0.1:8000/docs>.

**Frontend** (terminal 2):

```bash
cd image-to-ppt/frontend
npm install                                 # first time, or after package.json changes
npm run dev                                 # http://localhost:5173/
```

**Production build of the frontend** (static files, then serve them however you
like):

```bash
cd image-to-ppt/frontend
npm run build                               # type-checks, then writes dist/
npm run preview                             # serves dist/ on :5173
```

`npm run preview` has no `/api` proxy. The client defaults to the relative base
`/api`, so for a previewed or separately hosted build, point it straight at the
backend at build time - **including the `/api` path**:

```bash
VITE_API_BASE=http://localhost:8000/api npm run build
npm run preview                             # http://localhost:5173/
```

Those calls are cross-origin, which is exactly what the backend's CORS
allow-list (`http://localhost:5173`, `http://localhost:3000`) exists for.

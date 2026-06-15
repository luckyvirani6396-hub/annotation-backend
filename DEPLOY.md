# Deployment Guide

This backend is a **long-running FastAPI service** with MongoDB, Redis, Celery, and local file storage. It is **not compatible with Vercel serverless**.

Use **Docker Compose** locally and **Railway** or **Render** for production.

---

## Local development (Docker Compose)

```bash
cp .env.example .env
# Edit .env if needed (defaults work with compose service names)

docker compose up --build
```

| Service        | URL                          |
|----------------|------------------------------|
| API + Swagger  | http://localhost:9000/docs   |
| Health check   | http://localhost:9000/health |
| Flower (opt.)  | http://localhost:5555        |

Optional Celery monitoring:

```bash
docker compose --profile monitoring up
```

Default admin (change before any public deploy):

- Email: `admin@annotation.studio`
- Password: value of `ADMIN_PASSWORD` in `.env`

---

## Prerequisites for cloud deploy

1. **MongoDB Atlas** (free tier) — create a cluster and copy the connection string.
2. **Redis** — use Render Redis or Railway Redis plugin.
3. **Persistent disk** — mount at `/app/uploads` (uploads and Celery staging must live on the same filesystem as the worker).

---

## Deploy to Render

1. Push this repo to GitHub (without `.env`, `.venv`, `uploads/`, or `logs/`).
2. In [Render](https://render.com) → **New** → **Blueprint** → select this repo.
3. Render reads `render.yaml` and creates:
   - Web service (API + Celery worker in one container)
   - Managed Redis
   - 10 GB persistent disk on `/app/uploads`
4. Set these manually in the web service **Environment** tab:
   - `MONGODB_URL` — Atlas connection string
   - `ADMIN_PASSWORD` — strong password
   - `CORS_ORIGINS` — your frontend URL(s), comma-separated
   - `ASSET_BASE_URL` — public URL of this service (e.g. `https://annotation-api.onrender.com`)
5. Deploy. First boot seeds the admin user and connects to Atlas.

**Note:** Free Render web services spin down after inactivity; first request may be slow.

---

## Deploy to Railway

1. Push this repo to GitHub.
2. In [Railway](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
3. Railway uses `railway.toml` + `Dockerfile`.
4. Add a **Redis** plugin; map variables:
   - `REDIS_URL` → Redis URL (database `/0`)
   - `CELERY_BROKER_URL` → same host, database `/1`
   - `CELERY_RESULT_BACKEND` → same host, database `/2`
5. Add a **Volume** mounted at `/app/uploads`.
6. Set variables on the service:

   | Variable          | Example                                      |
   |-------------------|----------------------------------------------|
   | `MONGODB_URL`     | `mongodb+srv://user:pass@cluster.mongodb.net` |
   | `JWT_SECRET`      | `openssl rand -hex 48`                       |
   | `ADMIN_PASSWORD`  | strong password                              |
   | `CORS_ORIGINS`    | `https://your-app.vercel.app`                |
   | `ASSET_BASE_URL`  | `https://your-service.up.railway.app`        |

7. Deploy. Railway injects `PORT` automatically; `start-combined.sh` runs API + worker together.

---

## Frontend (Vercel)

Deploy the React frontend on Vercel as usual. Set:

```
REACT_APP_API_URL=https://your-api.onrender.com
```

(or your Railway URL). Point `CORS_ORIGINS` on the backend at your Vercel domain.

---

## Architecture notes

| Mode              | When to use                                      |
|-------------------|--------------------------------------------------|
| Docker Compose    | Local dev; separate API + worker containers      |
| Combined (PaaS)   | Railway / Render — one disk, API + worker process |
| Split API/worker  | Only with shared storage (NFS/S3); not default   |

---

## Security checklist

- [ ] Rotate `JWT_SECRET` and `ADMIN_PASSWORD` (old values were committed in `.env`)
- [ ] Never commit `.env` — use `.env.example` as a template
- [ ] Restrict `CORS_ORIGINS` in production
- [ ] Use MongoDB Atlas IP allowlist or VPC peering
- [ ] Enable Flower basic auth if exposing port 5555

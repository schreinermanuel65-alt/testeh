# Multiloader Release API — starter

A small FastAPI release service for ordinary, authorized software distribution.
It supports the six requested product categories and stores build files in a **private Supabase Storage bucket**. Metadata is stored in Supabase Postgres. The C++ and Python clients use the same HTTPS endpoints.

This is a starter for testing, not a hardened production licensing system. The client API key is a shared secret and can be extracted from a client binary. For real user licensing, replace it with per-user accounts or short-lived signed tokens. Do not use this as a mechanism to evade game security or anti-cheat systems.

## 1. Create Supabase resources

1. Create a project at https://supabase.com/
2. In Storage, create a **private** bucket named `releases`.
3. In Project Settings, find the database connection string. Use the direct or session-pooler connection string compatible with your host.
4. Copy the project URL and the `service_role` key from API settings. **Never put the service-role key in your loader or publish it.** It belongs only in the API server's environment variables.

The API uses the service-role key on the server to read/write the private bucket. Do not create a public bucket for private releases.

## 2. Deploy on Render

1. Upload this folder to a **private GitHub repository**.
2. Create a Render account at https://render.com/
3. Choose **New → Blueprint** and connect the repository, or create a Python web service using the included `render.yaml`.
4. Add these environment variables in Render:
   - `DATABASE_URL`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `ADMIN_API_KEY` (long random secret)
   - `CLIENT_API_KEY` (different long random secret)
5. Deploy. Test `https://YOUR-SERVICE.onrender.com/health`.

The free Render service can sleep when idle, and local files are ephemeral. This project keeps release files in Supabase Storage rather than on the Render filesystem. Free hosting is best for a small test and has usage/availability limits.

## 3. Publish a build

Use `curl` from a terminal, replacing the API URL and admin key:

```bash
curl -X POST "https://YOUR-SERVICE.onrender.com/api/v1/admin/products/fivem/releases" \
  -H "X-Admin-Key: YOUR_ADMIN_API_KEY" \
  -F "version=1.0.0" \
  -F "file=@./MyProgram.exe"
```

Supported product IDs:

- `fivem`
- `roblox`
- `fortnite`
- `cs2`
- `mega-chameleon`
- `war-dogs`

A product can have one release per version string. Use a new version for each upload.

## 4. Check latest version

```bash
curl "https://YOUR-SERVICE.onrender.com/api/v1/products/fivem/latest" \
  -H "X-Client-Key: YOUR_CLIENT_API_KEY"
```

## 5. Download a release

The latest endpoint returns the release `id`. Then:

```bash
curl -L "https://YOUR-SERVICE.onrender.com/api/v1/releases/RELEASE_UUID/download" \
  -H "X-Client-Key: YOUR_CLIENT_API_KEY" \
  -o downloaded-build.bin
```

The endpoint returns the artifact as a download. Check the SHA-256 value against the metadata before trusting a downloaded file.

## Notes and limitations

- Upload size is limited to 100 MB in this starter and upload content is buffered in memory. For large builds, implement direct/resumable uploads to object storage.
- `X-Client-Key` is a shared client key, **not a per-user licensing system**. Anyone who extracts it from a client could reuse it. For production, add individual user authentication, product entitlements, rate limits, audit logs, and short-lived download URLs.
- TLS protects data in transit; it does not make a downloaded program impossible to copy or inspect.
- Keep the Supabase service-role key and admin key secret. Never compile them into C++ or Python client applications.
- This service handles ordinary software release delivery. It does not implement stealth loading, process injection, or anti-cheat evasion.

# Certificate PDF Generation API

Production-ready Flask API that generates multi-page landscape A4 certificate PDFs from PNG templates. Designed for **n8n** HTTP workflows and deployment on **Render** or **Railway** (free tiers).

## Project structure

```text
project/
├── app.py
├── requirements.txt
├── Procfile
├── README.md
├── templates/
│   ├── page1.png
│   └── page2.png
├── generated/
│   └── .gitkeep
└── fonts/
    ├── arial.ttf
    └── arialbd.ttf
```

## Local setup

### Prerequisites

- Python 3.11 or newer
- `templates/page1.png` and `templates/page2.png` (included as placeholders; replace with your designs)
- `fonts/arial.ttf` (and optional `fonts/arialbd.ttf` for bold text)

### Install and run

```bash
cd project
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Optional: set public URL for pdfUrl in API responses
export PUBLIC_BASE_URL=http://127.0.0.1:5000

python app.py
```

The API listens on `http://0.0.0.0:5000` (override with `PORT`).

### Production-style local run (gunicorn)

```bash
export PORT=5000
gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 app:app
```

## API

### `POST /generate-certificate`

**Content-Type:** `application/json`

| Field | Required | Description |
|-------|----------|-------------|
| `certificateId` | Yes | Internal reference ID |
| `candidateName` | Yes | Displayed large on page 1 |
| `courseName` | Yes | Course title on page 1 |
| `grade` | Yes | Shown on page 2 |
| `certificateNumber` | Yes | Certificate number on page 1 |
| `delegateNumber` | Yes | Delegate ID on page 2 |
| `verifyUrl` | Yes | URL encoded in QR code (http/https) |
| `issueDate` | No | Defaults to current UTC date (`DD Month YYYY`) |

**Success (201):**

```json
{
  "success": true,
  "pdfUrl": "https://your-domain.com/generated/a1b2c3d4-....pdf",
  "filename": "a1b2c3d4-....pdf",
  "certificateId": "test-cert-1002"
}
```

### `GET /generated/<filename>`

Serves a generated PDF (UUID filename only).

### `GET /health`

Health check for load balancers.

## Adjusting text positions

Edit the `Page1Layout` and `Page2Layout` classes at the top of `app.py`. Coordinates use ReportLab points from the **bottom-left** of each landscape A4 page (842 × 595 pt).

## Example cURL request

```bash
curl -X POST http://127.0.0.1:5000/generate-certificate \
  -H "Content-Type: application/json" \
  -d '{
    "certificateId": "test-cert-1002",
    "candidateName": "Jane Doe",
    "courseName": "HACCP Food Safety Standards (Level 2)",
    "grade": "85%",
    "certificateNumber": "2026-05-101-001/123",
    "delegateNumber": "2026-0042-123",
    "verifyUrl": "https://example.com/certificates/verify?delegate=2026-0042-123"
  }'
```

Download the PDF:

```bash
curl -O "http://127.0.0.1:5000/generated/<filename-from-response>.pdf"
```

## Example n8n HTTP Request node

| Setting | Value |
|---------|--------|
| Method | `POST` |
| URL | `https://your-app.onrender.com/generate-certificate` |
| Authentication | None (or add API key in front of app if you extend it) |
| Send Body | Yes |
| Body Content Type | JSON |

**JSON body:**

```json
{
  "certificateId": "{{ $json.certificateId }}",
  "candidateName": "{{ $json.candidateName }}",
  "courseName": "{{ $json.courseName }}",
  "grade": "{{ $json.grade }}",
  "certificateNumber": "{{ $json.certificateNumber }}",
  "delegateNumber": "{{ $json.delegateNumber }}",
  "verifyUrl": "https://example.com/certificates/verify?delegate={{ $json.delegateNumber }}"
}
```

Map expressions to your workflow fields. Use the returned `pdfUrl` in a follow-up node (email, storage, etc.).

## Deploy on Render

1. Push this repository to GitHub.
2. In [Render](https://render.com), create a **Web Service**.
3. Connect the repo.
4. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 app:app`
   - Or use the included **Procfile** (Render detects it automatically).
5. **Environment variables:**
   - `PUBLIC_BASE_URL` = `https://your-service-name.onrender.com`
   - `PYTHON_VERSION` = `3.11.9` (optional, in Render dashboard)
6. Deploy. Note: free tier spins down after inactivity; first request may be slow.

**Important:** Generated PDFs live on the container filesystem. On Render free tier, files are **lost on redeploy** and **not shared across instances**. For production persistence, add object storage (S3, R2, etc.) or a mounted disk on a paid plan.

## Deploy on Railway

1. Push to GitHub and create a new project in [Railway](https://railway.app).
2. Add service from repo; Railway detects Python.
3. Set start command (if not from Procfile):

   ```bash
   gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 app:app
   ```

4. Variables:
   - `PUBLIC_BASE_URL` = your Railway public URL (e.g. `https://your-app.up.railway.app`)
5. Generate domain under **Networking** → **Public Networking**.

Same persistence caveat applies: use external storage for long-lived certificates.

## Environment variables

| Variable | Description |
|----------|-------------|
| `PORT` | HTTP port (set by Render/Railway) |
| `PUBLIC_BASE_URL` | Base URL for `pdfUrl` in JSON responses |
| `FLASK_DEBUG` | Set to `1` for local debug mode only |

## License note on fonts

`arial.ttf` / `arialbd.ttf` are proprietary on many systems. For redistribution, replace them with an open font (e.g. Liberation Sans) and update paths in `app.py`.

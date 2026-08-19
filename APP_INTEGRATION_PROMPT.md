# App integration prompt (paste into your app project)

Copy everything below into your app project's AI / developer chat.

---

Connect this app to our Certificate PDF Generation API (already deployed on Vercel).

## Goal

After SSO login, when a user clicks **Generate Certificate**, this app must:

1. Read the student/certificate row from the database (or logged-in user profile).
2. POST JSON to the certificate API.
3. Show or download the returned `pdfUrl`.

Do **not** generate PDFs inside this app. Do **not** redesign certificates.

## API

- **Base URL:** `https://certificate-generation-navy.vercel.app`
- **Generate:** `POST /generate-certificate`
- **List courses:** `GET /templates`
- **Health:** `GET /health`
- **Headers:** `Content-Type: application/json`
- **CORS:** enabled (browser can call directly)

## Professional Plumbing certificate

Always send for plumbing courses:

```json
{
  "courseName": "Professional Plumbing Training Program",
  "templateFile": "Professional plumbing tarining program.pdf"
}
```

## Required JSON fields

| Field | Source (DB / SSO) | Notes |
|-------|-------------------|--------|
| `certificateId` | uid or row id | Internal reference |
| `candidateName` | student full name | e.g. `Dilwinder Singh` |
| `courseName` | course title | Must match template |
| `grade` | grade or `"Excellent"` | Required by API |
| `certificateNumber` | cert number | e.g. `ET/PPT/001/2026` |
| `delegateNumber` | same as uid | Required by API |
| `verifyUrl` | verify link | Must start with `http://` or `https://` |
| `uid` | student UID | Printed after `UID:` |
| `issueDate` | issue date | Format `DD-MM-YYYY` |
| `startDate` | training start | Format `DD-MM-YYYY` |
| `trainingDuration` | months | Number only, e.g. `"3"` |

## Optional — photo & QR (from browser / database URLs)

Send **public https URLs** (recommended for frontend):

| Field | Example |
|-------|---------|
| `photoUrl` | `https://your-cdn.com/students/21PLM001.jpg` |
| `qrUrl` | `https://your-cdn.com/qr/21PLM001.png` |

Or send base64 (if image is uploaded in the app):

| Field | Example |
|-------|---------|
| `photoBase64` | data URL or raw base64 string |
| `qrBase64` | data URL or raw base64 string |

Do **not** send local file paths like `/Users/...` from the browser.

## Example request

```json
{
  "certificateId": "21PLM001",
  "candidateName": "Dilwinder Singh",
  "courseName": "Professional Plumbing Training Program",
  "templateFile": "Professional plumbing tarining program.pdf",
  "grade": "Excellent",
  "certificateNumber": "ET/PPT/001/2026",
  "delegateNumber": "21PLM001",
  "uid": "21PLM001",
  "verifyUrl": "https://sftlms.com/verify/21PLM001",
  "issueDate": "27-09-2026",
  "startDate": "15-08-2026",
  "trainingDuration": "3",
  "photoUrl": "https://YOUR-CDN/student-photo.jpg",
  "qrUrl": "https://YOUR-CDN/qrcode.png"
}
```

## Example success response (201)

```json
{
  "success": true,
  "pdfUrl": "https://certificate-generation-navy.vercel.app/generated/xxxxx.pdf",
  "filename": "xxxxx.pdf",
  "certificateId": "21PLM001",
  "templateFile": "Professional plumbing tarining program.pdf",
  "courseName": "Professional Plumbing Training Program"
}
```

## Frontend implementation

1. Add env variable:
   - Vite: `VITE_CERTIFICATE_API_URL=https://certificate-generation-navy.vercel.app`
   - Next.js: `NEXT_PUBLIC_CERTIFICATE_API_URL=https://certificate-generation-navy.vercel.app`

2. Create `generateCertificate(data)` service:

```javascript
export async function generateCertificate(payload) {
  const base = import.meta.env.VITE_CERTIFICATE_API_URL
    || process.env.NEXT_PUBLIC_CERTIFICATE_API_URL
    || "https://certificate-generation-navy.vercel.app";

  const res = await fetch(`${base}/generate-certificate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const json = await res.json();
  if (!res.ok || !json.success) {
    throw new Error(json.error || "Certificate generation failed");
  }
  return json;
}
```

3. On button click after SSO:
   - Load student from DB
   - Build JSON from table columns
   - Call `generateCertificate(payload)`
   - Open `pdfUrl` in new tab or trigger download

## n8n (optional middle layer)

If the app calls n8n instead of Vercel directly:

- App → `POST https://YOUR-N8N/webhook/generate-certificate`
- n8n Webhook → HTTP Request → `POST https://certificate-generation-navy.vercel.app/generate-certificate`
- Return `pdfUrl` back to the app

## Vercel env (already on certificate repo)

Set in Vercel dashboard for the certificate API project:

- `PUBLIC_BASE_URL` = `https://certificate-generation-navy.vercel.app`

---

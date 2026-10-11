# Setting up the real integrations (owner steps)

Defaults are fakes. Switch on one integration at a time, run its live check, then decide about the next.
Put every value in `.env` (git-ignored). Never paste keys into chat.

## Google Calendar (test calendar only)
1. console.cloud.google.com: create a project `p03-ops-agent`; APIs & Services > Library > enable **Google Calendar API**.
2. IAM & Admin > Service Accounts > create `p03-calendar`. Keys > Add key > JSON. Save the file OUTSIDE the repo, e.g. `~/secrets/p03-calendar.json`.
3. In calendar.google.com create a NEW calendar named `P03 test`. Settings for that calendar > Share with specific people > add the service account's email (`...@....iam.gserviceaccount.com`) with **Make changes to events**.
4. Same page > Integrate calendar > copy the **Calendar ID**.
5. `.env`: `GOOGLE_CREDENTIALS_FILE=/home/<you>/secrets/p03-calendar.json`, `GOOGLE_CALENDAR_ID=...`. Leave `CALENDAR_BACKEND=fake` until the check passes.
6. Check: `uv run python -m scripts.live_integrations google` (read-only), then `... google --write` (one test event, created and deleted).

## HubSpot (free account)
1. developers.hubspot.com: create a free developer/test account (or use your free CRM).
2. Settings > Integrations > Private Apps > Create. Scopes: `crm.objects.contacts.read/write`, `crm.objects.deals.read/write`.
3. Copy the token to `.env` as `HUBSPOT_TOKEN`. Check: `uv run python -m scripts.live_integrations hubspot --write`.

## Twilio (trial; messages only to your own verified phone)
1. console.twilio.com: copy Account SID and Auth Token to `.env` (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`).
2. Trial number > `TWILIO_SMS_FROM`. Or WhatsApp sandbox: Messaging > Try it out > WhatsApp, send the join code from your phone; sandbox number > `TWILIO_WHATSAPP_FROM`.
3. `TWILIO_ALLOWED_TO=+1XXXXXXXXXX` (your verified phone, the only number that may be texted).
4. Check: `uv run python -m scripts.live_integrations twilio --write`.
5. Go-live reminder: US SMS to the public needs A2P 10DLC registration; not needed for this demo.

## Turning it on for the app
`CALENDAR_BACKEND=google`, `CRM_BACKEND=hubspot`, `TWILIO_LIVE=true`. Real WRITES also need `DRY_RUN=false`; keep it true until you say so.

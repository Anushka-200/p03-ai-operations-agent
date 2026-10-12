# Setting up the real integrations (owner steps)

Defaults are fakes. Switch on ONE integration at a time, run its live check, then decide about the next.
Put every value in `.env` (git-ignored). Never paste keys into chat. Website menus change now and then;
if a label differs slightly, look for the closest one.

## Where each value goes in `.env`

| What you copy | Where you get it | `.env` variable |
|---|---|---|
| Path of the Google key file | You download it (Google step 2) | `GOOGLE_CREDENTIALS_FILE` |
| Calendar ID of the test calendar | Google Calendar > calendar settings > Integrate calendar | `GOOGLE_CALENDAR_ID` |
| HubSpot private-app token | HubSpot > private app > Auth tab > Show token | `HUBSPOT_TOKEN` |
| Twilio Account SID | console.twilio.com home page > Account Info | `TWILIO_ACCOUNT_SID` |
| Twilio Auth Token | same box (click the eye icon) | `TWILIO_AUTH_TOKEN` |
| Twilio trial phone number | Phone Numbers > Manage > Active numbers | `TWILIO_SMS_FROM` (format `+1XXXXXXXXXX`) |
| WhatsApp sandbox number | Messaging > Try it out > Send a WhatsApp message | `TWILIO_WHATSAPP_FROM` (format `+14155238886`, no `whatsapp:` prefix) |
| YOUR phone number | you | `TWILIO_ALLOWED_TO` (format `+91XXXXXXXXXX`; the ONLY number that can be texted) |

Switches (leave all off until its check passes): `CALENDAR_BACKEND=fake|google`, `CRM_BACKEND=fake|hubspot`,
`TWILIO_LIVE=false|true`, `DRY_RUN=true` (stays true until you say so).

## 1. Google Calendar (a throw-away TEST calendar only)
1. Open **console.cloud.google.com** and sign in with your Google account.
2. Top bar > project picker > **New project** > name `p03-ops-agent` > Create. Make sure it is selected.
3. Left menu > **APIs & Services > Library** > search **Google Calendar API** > **Enable**.
4. Left menu > **IAM & Admin > Service Accounts** > **Create service account** > name `p03-calendar` > Create and continue > skip the roles > Done.
5. Click the new account > **Keys** tab > **Add key > Create new key > JSON > Create**. A `.json` file downloads.
   Move it OUTSIDE the project, for example `~/secrets/p03-calendar.json` (WSL: `mkdir -p ~/secrets && chmod 700 ~/secrets`, then copy the file there from Windows `Downloads`, e.g. `cp /mnt/c/Users/<you>/Downloads/<file>.json ~/secrets/p03-calendar.json && chmod 600 ~/secrets/p03-calendar.json`).
   Note the service account's e-mail (looks like `p03-calendar@p03-ops-agent.iam.gserviceaccount.com`).
6. Open **calendar.google.com** > left side "Other calendars" **+ > Create new calendar** > name `P03 test` > Create.
7. Hover the new calendar > three dots > **Settings and sharing**.
   - **Share with specific people or groups > Add people** > paste the service-account e-mail > permission **Make changes to events** > Send.
   - Scroll to **Integrate calendar** > copy the **Calendar ID** (ends with `@group.calendar.google.com`).
8. In `.env`: `GOOGLE_CREDENTIALS_FILE=/home/anus11kulkarni/secrets/p03-calendar.json` and `GOOGLE_CALENDAR_ID=<the id>`.
9. Check: `uv run python -m scripts.live_integrations google` (read-only), then `... google --write` (creates and deletes ONE test event).
10. Then in `.env`: `CALENDAR_BACKEND=google`.

## 2. HubSpot (free account)
1. Open **app.hubspot.com** and sign up for the free CRM (or use a free developer test account at developers.hubspot.com).
2. Top-right gear **Settings > Integrations > Private Apps** (HubSpot may show it under **Development > Legacy apps**) > **Create private app**.
3. Name `p03-ops-agent`. **Scopes** tab > add `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.deals.read`, `crm.objects.deals.write` > **Create app > Continue creating**.
4. On the app page open the **Auth** tab > **Show token** > copy it.
5. In `.env`: `HUBSPOT_TOKEN=<the token>`.
6. Check: `uv run python -m scripts.live_integrations hubspot --write` (one test contact + deal, upserted twice to prove no duplicate). Delete the `P03 Livetest` contact and deal in HubSpot afterwards if you want a clean account.
7. Then in `.env`: `CRM_BACKEND=hubspot`.

## 3. Twilio (trial; messages only to your own verified phone)
1. Open **twilio.com**, sign up (trial). Verify your own phone number when asked (this is also your **Verified Caller ID**).
2. Console home page > **Account Info**: copy **Account SID** and **Auth Token** (click the eye) into `.env` as `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`.
3. SMS: **Phone Numbers > Manage > Buy a number** (the trial gives one free number; pick one with SMS) > copy it into `TWILIO_SMS_FROM`. A US number needs no registration for trial messages to your verified phone.
4. WhatsApp (optional, easiest for non-US phones): **Messaging > Try it out > Send a WhatsApp message**. From YOUR phone send the shown join code (`join <two words>`) to the sandbox number (usually `+1 415 523 8886`). Put that number in `TWILIO_WHATSAPP_FROM` as `+14155238886`.
5. `TWILIO_ALLOWED_TO=+<your number with country code>`.
6. Check: `uv run python -m scripts.live_integrations twilio --write` (sends ONE message to that number).
7. Then in `.env`: `TWILIO_LIVE=true`. Real sending ALSO needs `DRY_RUN=false`; keep it true until you say so.
8. Inbound replies from your phone to the agent need a public webhook URL (a tunnel). That is NOT set up and needs your separate approval. The demo works without it: we post signed fake webhooks locally.
9. Go-live reminder: US SMS to the public needs A2P 10DLC registration (days to weeks); not needed for this demo.

## 4. Telegram
Bot token and `TELEGRAM_OWNER_ID` go in `.env`. `TELEGRAM_LIVE=true` makes the bot send for real (messages go only to your own id).

## Turning real writes on
`DRY_RUN=false` is what lets the system really create calendar events, CRM records and messages. It is a single switch
that changes behaviour for every integration whose own switch is also on. Tell me when you want it; I will run one
integration at a time.

# Notifications & payments — test mode

## Paystack (not live yet)

With the defaults below, booking **skips Paystack** and issues the QR ticket immediately:

```env
PAYSTACK_MOCK=true
PAYSTACK_AUTO_CONFIRM=true
```

Flow after you tap **Confirm booking**:

1. Backend creates the booking and mock payment  
2. Backend settles payment → confirms booking → issues HMAC QR ticket  
3. Sends ticket email (passenger email) + confirmation SMS (passenger phone)  
4. Schedules reminders at **24h, 3h, 1h** before departure  
5. Web redirects to `/booking/{ref}?new=1` (ticket screen)

When Paystack is ready later: set `PAYSTACK_MOCK=false`, put real keys in env, and set `NEXT_PUBLIC_PAYSTACK_PUBLIC_KEY` on the web app.

## Emails

| Event | Template | Channel |
|---|---|---|
| Account registered | `registration_welcome.html` | Email to registered address |
| Booking paid / mock-confirmed | `booking_confirmed.html` (+ QR inline) | Email + SMS |
| 1 hour before departure | `trip_reminder.html` | Email + **SMS to registered phone** |
| 24h / 3h reminders | same reminder template | Email + SMS |

Local preview without SMTP:

```env
EMAIL_PROVIDER=file
EMAIL_FILE_DIR=./storage/emails
```

HTML files appear under `storage/emails/`. For real delivery use `EMAIL_PROVIDER=smtp` (Gmail app password, SendGrid, or Resend).

## Do you need Twilio?

| Use case | Need Twilio? | What to use |
|---|---|---|
| Local testing SMS | **No** | `SMS_PROVIDER=console` (logs the text) |
| Live SMS to Nigerian phones | **No** (preferred) | **Termii** — `SMS_PROVIDER=termii` + `TERMII_API_KEY` |
| SMS via Twilio | Optional | `SMS_PROVIDER=twilio` + Account SID / Auth Token / From number |
| WhatsApp booking bot (`Ecojindu_API`) | **Yes** (later) | Twilio WhatsApp Business / sandbox |

You only need Twilio when you turn on the WhatsApp bot in `Ecojindu_API`. For the **1-hour trip reminder SMS**, Termii or console is enough.

## Reminder schedule

`REMINDER_OFFSETS_HOURS=24,3,1` — APScheduler polls these windows (or Cloud Tasks if enabled). The 1h SMS goes to `booking.passenger_phone`.

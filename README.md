# eFootball Marketplace Telegram Bot

This service upgrades the supplied Aiogram bot without replacing its SQLite data
model or existing seller/admin workflows. The marketplace is intentionally
channel-only: `/marketplace` and the Marketplace menu link to
`https://t.me/VXStorePes`; account listings and photos are never sent to buyers
in private chat.

## Configure

Add these values in Replit Secrets/environment variables:

- `BOT_TOKEN` — Telegram token from BotFather (secret).
- `ADMIN_ID` — the authorized administrator's numeric Telegram user ID.
- `CHANNEL_ID` — `@VXStorePes` by default; the bot must be an administrator
  with permission to post.
- `ADMIN_CONTACT_USERNAME` — support contact without the `@` prefix.
- `DATABASE_PATH` — defaults to `marketplace.sqlite3`.

Use `.env.example` as a reference only. Do not commit a real `.env` file or bot
token.

## Run

Install the project dependencies from `pyproject.toml`, configure the
environment values above, and run:

```sh
python -m efootball_marketplace_bot.bot
```

The bot uses long polling. A Replit workflow can run it as a background
process; 24/7 availability depends on a deployment/runtime that supports
long-running workers and is not guaranteed by the editor workflow.

## Channel setup and smoke test

1. Add the bot as an administrator of `@VXStorePes` with permission to post.
2. Start the bot and send `/start`; a new user chooses English or Arabic.
3. Tap Marketplace and confirm it opens the channel URL without sending any
   listing or photo to the private chat.
4. Submit an account from a test user, then approve it from the configured
   admin account and confirm that the album appears in the channel.
5. Test a custom-price offer and accept/decline it from the seller account.

## Database safety

The configured SQLite file is persistent application data. Keep it in a
durable Replit volume/path and back it up before replacing or moving the bot.
Schema changes are additive; existing user, submission, moderation, settings,
and notification rows are retained. Never share the database backup publicly:
it contains Telegram user IDs and seller submission data.

Administrators can download a point-in-time copy with `/backup`. The bot also
creates a backup before applying its additive schema upgrade.

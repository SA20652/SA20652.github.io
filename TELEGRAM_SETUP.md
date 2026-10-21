# Telegram Mini App / group support

The bot now supports `/play` and `/balance` in group chats.

For the best group-chat experience, configure the bot's Main Mini App (or a named Mini App) in BotFather and set:

- `WEBAPP_URL=https://YOUR_PUBLIC_APP_HOST/`
- optional `WEBAPP_SHORT_NAME=your_mini_app_short_name`

When `WEBAPP_SHORT_NAME` is set, group `/play` buttons use the Telegram direct Mini App link. Without it, the bot falls back to the Main Mini App direct link.

The WebApp and bot use the same SQLite database and Telegram user ID, so balance, cases, inventory and game progress remain synchronized.

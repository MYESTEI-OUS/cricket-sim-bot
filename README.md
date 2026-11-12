# Cricket Simulator Telegram Bot

A Python Telegram bot for multiplayer and single-player cricket. In group multiplayer, two captains choose teams and XIs; in Vs Bot, one player chooses a team and XI while the bot selects an opponent and plays the other side. Both modes support international and IPL matches, overs and pitch selection, interactive player decisions, and a live scorecard.

## Set up the Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather), run `/newbot`, and follow its instructions.
2. Copy the token BotFather gives you.
3. Copy `.env.example` to `.env` and add your token after `TELEGRAM_BOT_TOKEN=`. Keep `.env` private.
4. If your environment uses a TLS inspection proxy or self-signed certificate, set `TELEGRAM_SSL_VERIFY=false` before starting the bot so Python will allow Telegram's HTTPS connection. Otherwise leave it unset for normal verification.
5. Install Python 3.10 or newer. This bot uses only Python's standard library, so no packages need installing.
6. From this folder, run:

   ```sh
   python3 -m unittest discover -s test -v
   python3 -m src.bot
   ```

7. Open your bot in Telegram and send `/start`.

## Play

- In a group, send `/playint` for an international multiplayer match or `/playipl` for IPL. A second member joins as away captain.
- In a private chat, send `/vsbot` for international cricket or `/vsbotipl` for IPL. Choose your team and XI; the bot automatically selects the opponent and its XI.
- In either mode, the host/player chooses 2, 5, 10, or 20 overs and a pitch: balanced, batting paradise, green seamer, or turning track.
- The home team bats first. The batting captain chooses Aggressive, Defensive, Ultra aggressive, Hit gaps, or Rotate strike; the bowling captain then chooses Aggressive, Defensive, Variation, or Knuckle ball.
- The styled scorecard, batter figures, and bowling figures update after each ball, with run rates and chase details. The innings ends after the selected overs or 10 wickets; the away side chases the target.
- The bundled international and IPL franchise player squads are starter lists, not live/current team sheets. Active match state is saved locally in `bot_state.sqlite3` and restored when the bot restarts; keep this file with the bot if moving machines.
- Use `/cancel` to stop a match (host only), `/help` for a reminder, `/ping` to check that the bot is online, `/members` to display the group member count, and `/refresh` to redraw the active lobby or live match screen.

For always-on hosting, run the bot on a stable server or cloud VM, keep the token in a private `.env`, and restart it automatically if needed. The bot itself supports uptime checks and group member lookup for operational monitoring.

The bot needs an internet connection and a valid BotFather token to connect to Telegram.

## Deploy on Render's free plan

The Render Blueprint in `render.yaml` creates a free Python web service. On Render, the bot switches from long polling to Telegram webhooks and serves `/health` for service checks. Add `TELEGRAM_BOT_TOKEN` as a secret environment variable in Render; never commit `.env` or paste the token into chat.

Render's free web services can spin down after 15 minutes without inbound traffic and do not include persistent disks. Telegram webhook traffic can wake the service, but startup may delay replies, and match state stored in SQLite may be lost when the service restarts or redeploys. Use a paid background worker or another persistent host for reliable always-on play and saved matches.

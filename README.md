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

For 24/7 hosting, run the bot on a stable server or cloud VM, keep the token in a private `.env`, and restart it automatically if needed. The bot itself supports uptime checks and group member lookup for operational monitoring.

The bot needs an internet connection and a valid BotFather token to connect to Telegram.

## Deploy continuously on Render

This project includes a Render Blueprint in `render.yaml` for a Python background worker. The worker runs the bot's long-polling process without needing an inbound web server.

1. Push this project to a GitHub or GitLab repository. Keep `.env` out of version control; add the bot token only in Render's environment settings.
2. In Render, choose **New → Blueprint**, connect the repository, and select `render.yaml`.
3. Enter `TELEGRAM_BOT_TOKEN` when Render prompts for the secret, then review the selected `0.5c-512mb` paid compute plan and current price before approving creation.
4. Wait for the deployment to finish and check the worker logs for `Cricket Simulator bot is running.`
5. Stop any copy of the bot still running on a local computer. Telegram long polling supports one active poller per bot token.

Render background workers do not have a free always-on plan. This deployment uses the smallest paid worker plan; charges are controlled by your Render account and begin only if you create the service. Hosting improves availability but cannot guarantee uninterrupted service. Match state is stored in the local SQLite database; persistent disks are required to keep it through Render redeploys.

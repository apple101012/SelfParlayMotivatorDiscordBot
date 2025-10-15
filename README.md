# Self Parlay DM Bot

A Discord bot that lets you bet points on your own tasks. Create a parlay of 1–5 legs, set a deadline in ET, mark each leg complete or fail, and win only if all legs are complete by the deadline. The bot works entirely in DMs and stores data in a local JSON file.

## Quick start

1. Clone this repository.
2. Put your bot token in a `.env` file: DISCORD_TOKEN=yourtoken
3. Run: python discordbot.py
4. Invite the bot to a server, then open a DM with the bot and use the commands below.


## Features

- DM-only slash commands
- Two-step leg update flow: pick a leg, then choose Complete or Fail
- Re-send active parlay cards with `/parlays`
- `/bank` shows balance, streak, cap usage, and next daily cap reset time
- Automatic resolution at the deadline
- Local JSON storage, no database

## Commands

- `/rules`  
  Overview, caps, cooldowns, and example usage.

- `/faq`  
  Short answers to common questions.

- `/bet <stake> <legs_text> <deadline>`  
  Create a parlay. Legs must be written in parentheses. Deadline must be ET in `MM/DD/YYYY HH:MM AM/PM`.

  Examples: 
- /bet 50 (go to gym) (study 40 mins) 10/14/2025 11:59 PM
- /bet 100 (finish 310 hw) 11/01/2025 10:00 PM


- `/parlays`  
Re-sends your active parlay cards in DM with fresh embeds and buttons.

- `/bank`  
Shows balance, win streak, daily and weekly stake usage, next daily reset, and last five results.

## Parlay flow in DMs

1. Run `/bet` to create a parlay. The bot replies with a parlay card that includes buttons.
2. Click **Modify a leg**  
 a. Select the specific leg to modify  
 b. Choose **Mark Complete** or **Mark Fail**  
 The parlay card updates immediately and is saved.
3. Click **Resolve Now** when all legs are complete. Otherwise, the bot auto-resolves at the deadline.

## Rules

- Start balance: 1000 points
- Daily stake cap: 150 points
- Weekly stake cap: 800 points
- Cooldown after loss: 60 minutes
- Maximum legs: 5
- Payout multipliers by leg count:
- 1 leg -> 1.20x
- 2 legs -> 1.50x
- 3 legs -> 1.80x
- 4 legs -> 2.00x
- 5 legs -> 2.20x
- Daily cap resets at midnight ET. `/bank` shows exact reset time.

## Data model

All data is stored in `selfparlay_data.json`.

Top-level keys:
- `users` - balance, stake usage, streaks
- `parlays` - parlay objects keyed by UUID
- `ledger` - recent point changes

Parlay structure (abridged):

```json
{
"id": "uuid",
"user_id": "discord_user_id",
"stake": 50,
"legs": [
  {"text": "go to gym", "status": "OPEN"}
],
"legs_count": 1,
"multiplier": 1.2,
"created_ts": "ISO",
"deadline_ts": "ISO",
"status": "ACTIVE",
"message_id": 1234567890,
"channel_id": 1234567890,
"resolved_ts": null
}
```
Notes

The bot is designed for DMs. If a command is used in a server, the bot nudges you to DM and sends instructions.

Interactions only present valid choices. A leg must be OPEN to be modified.
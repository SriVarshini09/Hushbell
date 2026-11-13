# Hushbell: 2-minute demo script

For the Do Not Disturb Hackathon. Theme: "Your phone is on Do Not Disturb. Your agent isn't."

## On the table

| What | Set-up |
|---|---|
| **Projector** | The laptop showing **http://localhost:8000/present** (the big-screen view). Optionally the parent's phone mirrored next to it with `scrcpy`. |
| **Your phone** (the parent's) | Do Not Disturb **on**. Hushbell's Twilio number saved as "Hushbell" and starred. Hushbell page open on it. |
| **Android with the school apps** | Plugged into the laptop, screen awake, Remind signed in as a parent. (If your own phone is Android, it's the same phone.) |
| **Teacher phone** | A teammate's phone with the Remind teacher account. Hand it to **judge 1**. |
| **Judge 2's own phone** | They will call Hushbell's number. Write the number on a card. |
| **Co-parent phone** | A teammate's phone, set as `HUSHBELL_PARTNER_PHONE`, so the room sees the text arrive. |
| **Laptop network** | A phone hotspot, not venue Wi-Fi. `ngrok http 8000` running in a second terminal. |

## Before you start (5 minutes)

1. `hushbell doctor --live` shows no problems, and `hushbell run` prints "Calls to +1… now reach Hushbell."
2. Press **Test call** on the page with Do Not Disturb on. Your phone rings.
3. Run the whole demo once. Then clear the phone's notifications and run `hushbell reset` (or press
   **Start over** when running with `--demo`), so the repeated messages count as new.
4. Have the backup video open in another tab.

## The script

| Time | Say and do | What the room sees on the big screen |
|---|---|---|
| 0:00 | "Parents juggle up to 15 school apps. The Boston Globe says many have stopped reading them. Buried in there is 'your kid has a fever.' And when the school nurse calls, it's from a number you don't know, on Do Not Disturb." | The empty presenter view: "the parent's phone is on Do Not Disturb" |
| 0:15 | **Judge 1** (teacher phone) sends two Remind messages: "Bake sale Friday, bring cookies" and "Field trip next Friday, permission form due today: (link)". | Two rows appear: bake sale → LATER, field trip → NEEDS YOU, with its plan: fill the form, add the trip to the calendar |
| 0:35 | **Judge 2** calls Hushbell's number: *"Hi, this is the school nurse at Lincoln. Anna has a fever. Can someone pick her up by 3?"* Hushbell answers on their speaker. | A "Phone call" row: URGENT, "Safety words: fever · no AI needed" |
| 0:50 | **Your phone rings**, on Do Not Disturb. Answer on speaker: Hushbell explains and offers "Press 1 and I will connect you to them, set an alarm, text Sam, and open directions." **Press 1.** | The steps tick ✓ one by one. Judge 2's phone rings: they're talking to you. Sam's phone gets the text |
| 1:25 | "At lunch I check once." On your phone, tap **Do these 2** on the field trip card. | The form gets filled and the trip lands on the calendar |
| 1:40 | Optional: call Hushbell from your own phone and ask *"What do I need to do this week?"* | Hushbell reads out the week |
| 1:50 | "A text, a call, and a form. One phone call to me, one tap, and I never opened a school app. Hushbell is open source and runs on my own phone and laptop." | The counters; the repo link |

## Likely questions

- **"Isn't this Fambot?"** Fambot reads accounts it can connect to and sends a checklist. Hushbell
  reads what lands on the phone, including apps with no integration, answers calls, acts inside the
  apps, and is the one caller allowed through Do Not Disturb.
- **"What if it misses an emergency?"** Health and safety words are caught by fixed rules before
  any AI. Callers can press 1 for an emergency. If Claude is down, messages wait for you instead of
  being dropped.
- **"What if it sends the wrong thing?"** Nothing is sent, signed or paid without a press of 1 or a
  tap. You see each step, can untick it, and can edit the reply first.
- **"Who can it text?"** Only the one co-parent you set, with your OK, showing the exact words.
- **"What about my personal messages?"** For WhatsApp and Gmail it only reads notifications that
  mention your school keywords. Everything is stored on your laptop.
- **"Why Android?"** It lets an agent read notifications and operate apps without jailbreaking.
  The calls and the page work with any phone, iPhone included.
- **"Who pays?"** Parents of K–8 kids, on a subscription like Fambot's; then schools and
  after-school programs.

## If something breaks

| Problem | Do this |
|---|---|
| A message doesn't appear | Check the "Phone connected" chip; run `hushbell notifications` |
| Hushbell doesn't answer judge 2's call | Check that ngrok is running and `hushbell run` printed "now reach Hushbell"; restart Hushbell after restarting ngrok |
| Your phone doesn't ring | Approve on the page instead: "the call is the shortcut, the page is the fallback" |
| A step shows ✗ on the big screen | Press **Try again**, or move on: "every step is logged, and it only retries what didn't finish" |
| Venue Wi-Fi is down | Switch the laptop to the phone hotspot and restart ngrok, then Hushbell |
| Everything is down | Play the backup video, and show the page in `--dry-run --demo` mode |

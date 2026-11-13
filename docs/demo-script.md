# Hushbell: 2-minute demo script

For the Do Not Disturb Hackathon. Theme: "Your phone is on Do Not Disturb. Your agent isn't."

## On the table

| What | Set-up |
|---|---|
| **Projector** | The laptop showing **http://localhost:8000/present** (the big-screen view), full screen with **F**. Optionally the parent's phone mirrored next to it with `scrcpy`. |
| **Your phone** (the parent's) | Do Not Disturb **on**. Hushbell's Twilio number saved as "Hushbell" and starred. Hushbell **installed as an app** (laptop page → Open on your phone → scan → Install). |
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
| 0:00 | "Parents juggle up to 15 school apps. The Boston Globe says many have stopped reading them. Buried in there is 'your kid has a fever.' And when the school nurse calls, it's from a number you don't know, on Do Not Disturb." | "All quiet." The moon: "Eli's phone · Do Not Disturb is on. Hushbell is not." |
| 0:15 | **Judge 1** (teacher phone) sends two Remind messages: "Bake sale next Friday, cookie donations welcome" and "Field trip next Friday, permission form due today: (link)". | Two rows slide in, "Reading it…" then: bake sale → LATER, field trip → NEEDS YOU, each "Sorted by Claude", with its plan: fill the form, add the trip to the calendar |
| 0:35 | **Judge 2** calls Hushbell's number: *"Hi, this is the school nurse at Lincoln. Anna has a fever. Can someone pick her up by 3?"* Hushbell answers on their speaker. | Blue banner: "Hushbell answered a call · listening". Then a "Phone call" row: URGENT, "Safety rule · before any AI" |
| 0:50 | **Your phone rings**, on Do Not Disturb. Answer on speaker: Hushbell explains and offers "Press 1 and I will connect you to them, set an alarm, text Sam, and open directions." **Press 1.** | Yellow ringing banner: "Ringing Eli through Do Not Disturb · Press 1 to …". Then green: "Eli pressed 1". The steps tick ✓ one by one. Judge 2's phone rings: they're talking to you. Sam's phone gets the text |
| 1:25 | "At lunch I check once." On your phone, tap **Do these 2** on the field trip card. | The form gets filled and the trip lands on the calendar |
| 1:40 | Optional: call Hushbell from your own phone and ask *"What do I need to do this week?"* | Hushbell reads out the week |
| 1:50 | "Three messages and a phone call. My phone rang once, I pressed one key and tapped once, and I never opened a school app. Hushbell is open source and runs on my own phone and laptop." | The four numbers: school messages → rang through → things done on the phone → taps by Eli |

## Cloud version (if you set up PhoneUse)

Same script, with the school apps on a PhoneUse cloud phone instead of an Android on the table.
Show the cloud phone in the PhoneUse console next to the big screen: the room sees Hushbell pull
the notification shade down, and later sees PhoneUse's agent open Remind and send the reply. The
big screen's header lists the partner tools that are on (PhoneUse, ElevenLabs, Tavily,
LangSmith, Replit). Line to say: "My phone has no school apps at all. They live on a cloud phone
that Hushbell watches for me."

For the "Ask" moment, try one that needs the web: *"What time does the science museum open on
the field trip day?"* The answer says it looked it up with Tavily.

## Likely questions

- **"Which partner tools did you use?"** PhoneUse (the cloud phone with the school apps), LangGraph
  and LangSmith (each message's flow and its trace), Replit (always-on hosting for the app and the
  calls), Tavily (Ask looks things up), ElevenLabs (the voice on the calls), and Claude for sorting
  and reading the screen.

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
| Everything is down | Play the backup video, or run `hushbell run --no-phone --dry-run --demo` and drive the big screen with keys **1–6** (0 starts over). With calls off, the fever row shows a red "Urgent · waiting for Eli" banner; approve it on the phone page and the steps tick off |

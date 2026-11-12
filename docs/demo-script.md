# Hushbell: 2-minute demo script

For the Do Not Disturb Hackathon. Theme: "Your phone is on Do Not Disturb. Your agent isn't."

## On the table

- **Parent's phone:** Android with a SIM, school apps signed in, Do Not Disturb on, Hushbell's
  number starred. Connected to the laptop by USB and mirrored with `scrcpy`.
- **Teacher's phone:** a teammate's phone with the Remind teacher account. Hand it to a judge.
- **Laptop:** `hushbell run` and the Hushbell page, on a phone hotspot (not venue Wi-Fi).

## Before you start

1. `hushbell doctor --live` shows no problems.
2. Press **Test call** on the page with Do Not Disturb on. The phone rings.
3. Run the whole demo once, then clear the phone's notifications.
4. Have the backup video (`docs/explainer.html` or the recording) open in another tab.

## The script

| Time | Say and do | What the room sees |
|---|---|---|
| 0:00 | "Parents juggle up to 15 school apps. The Boston Globe says many have stopped reading them. Buried in there is 'your kid has a fever.'" | The parent's phone, Do Not Disturb on |
| 0:20 | Hand the teacher phone to a judge: "You're the teacher. Send a bake sale reminder, then 'Field trip form due today' with this link, then 'Maya has a fever, please pick her up by 1 pm.'" | Messages appear on the Hushbell page within seconds; the phone stays silent |
| 0:40 | "Hushbell read those off the phone itself. No school login, no integration. The fever was caught by fixed safety rules before any AI ran. Claude sorted the rest." | Bake sale → Later. Field trip → Needs you. Fever → Urgent |
| 0:55 | The phone rings. "This is the only call allowed through Do Not Disturb." Answer on speaker, press 1. | Mirror: the agent opens Remind and types the reply. The judge's phone receives it |
| 1:25 | "At lunch I check once." Tap **Fill the form** on the field trip card. | Mirror: Chrome fills and submits the form |
| 1:45 | "Three messages became one call and one tap, and I never opened a school app. Hushbell is open source and runs on your own phone and laptop." | The counters on the page; the repo link |

## Likely questions

- **"Isn't this Fambot?"** Fambot reads accounts it can connect to. Hushbell reads what lands on the
  phone, including apps with no integration, then acts inside them and rings through Do Not Disturb.
- **"What if it misses an emergency?"** Health and safety words are caught by fixed rules before
  any AI. If Claude is down, messages are shown to you instead of dropped.
- **"What if it sends the wrong thing?"** Nothing is sent, signed or paid without a press of 1 or
  a tap, and you can edit the reply first.
- **"What about my personal messages?"** For WhatsApp and Gmail it only reads notifications that
  mention your school keywords. Everything is stored on your laptop.
- **"Why Android?"** It lets an agent read notifications and operate apps without jailbreaking.
  The same brain could drive an iPhone through Tapkit or a PhoneUse phone.
- **"Who pays?"** Parents of K–8 kids, on a subscription like Fambot's; then schools and
  after-school programs.

## If something breaks

| Problem | Do this |
|---|---|
| Message doesn't appear | Check the "Phone connected" chip; run `hushbell notifications` |
| Phone doesn't ring | Tap **Send reply** on the page instead; say "the call is the shortcut, the page is the fallback" |
| Agent gets stuck in the app | Press **Try again**, or show the backup video for that beat |
| Venue Wi-Fi is down | Switch the laptop to the phone hotspot and restart ngrok |

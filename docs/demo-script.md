# Hushbell: demo plan and script

For the Do Not Disturb Hackathon x Phone Use (EF San Francisco, Nov 13, demos after 8 hours of
building). Theme: "Your phone is on Do Not Disturb. Your agent isn't."

Planned with Claude Fable 5.2 from the code, then checked against it. **Nothing here has run on
a real Twilio account or a real PhoneUse cloud phone yet**: the first real test is the first job
on the day.

## The pitch

"Hushbell lets a parent keep Do Not Disturb on all day. An agent reads the school apps on a cloud
phone, and the only thing that can ring is your kid needing you. One keypress, and it's handled."

**The story.** Eli is a parent; Anna is in 3rd grade; Sam is the co-parent; Ms. Rivera is the
teacher. (These are the names in your `.env`, and they are what the screens show. Use them in
every line you say.) Before: Eli either drowns in school-app pings, or turns on Do Not Disturb and
also silences the school nurse calling from a number he doesn't know. After: Do Not Disturb stays
on, his phone has no school apps at all, the noise waits, a yes is one tap, and an emergency rings
through.

Open with the demonstration, not a statistic. Any number you say ("parents juggle N apps") needs a
source you can name if asked; if you don't have one with you, leave it out.

## What we demo: two separate rails

- **Rail A, the climax: the call.** A judge calls Hushbell's number as the school nurse, Eli's
  phone rings through Do Not Disturb, he presses 1 and is connected to the judge. This needs only
  Twilio and a public address, so it doesn't depend on the cloud phone.
- **Rail B, the PhoneUse act.** A real Remind message arrives on the PhoneUse cloud phone, Hushbell
  reads it off the notification shade, Eli taps once, and PhoneUse's agent types the reply in Remind.

Why: the PhoneUse founder is judging, so the cloud phone must do real work in front of him. But
the PhoneUse code has never touched a real key, so the climax must not depend on it. The
cloud-phone mode also makes the "everything from a phone" rule literally true: Eli's phone can be
an iPhone with no school apps. Skip the USB Android: it's also untested, adds a cable, and tells
the weaker story for this room.

**Go/no-go at 1:30 PM.** If reading the cloud phone and one reply run aren't working end to end by
then, Rail B becomes the demo keys (1–6 on the big screen) feeding the real pipeline (real Claude,
real Twilio). Say so plainly if asked.

## On the table

| What | Set-up |
|---|---|
| **Projector** | `/present` full screen (press **F**). Beside it, a narrow window with the PhoneUse console's live view of the cloud phone (check at 10 AM that the console has one). |
| **Eli's phone** (presenter's hand) | Do Not Disturb **on**. Hushbell's Twilio number saved as "Hushbell" and starred; starred contacts allowed through. Hushbell installed as an app. Ringer up. |
| **Teacher phone** (teammate) | Remind teacher account, with Eli's parent account (on the cloud phone) in the class. |
| **Judge's own phone** | They call Hushbell's number. Hand them a card with the number and one line to read. |
| **Laptop** | Phone hotspot, not venue Wi-Fi. `ngrok http 8000` and `hushbell run --demo` (real backends, with keys 1–6 as a fallback). |

The judge's card: *"Hi, this is the nurse at [your school]. Anna has a fever. Please pick her up
as soon as you can."* Say "as soon as you can", not a clock time. A clock time adds a "time to
leave" step: on a USB phone that's an alarm, but in cloud mode it's a reminder call that would ring
Eli's phone in the middle of judging.

## The script (3 minutes)

| Time | Say | Who does what | Screens |
|---|---|---|---|
| 0:00 | "This is my phone. Do Not Disturb is on, and there are no school apps on it." | Teammate calls Eli's phone from an ordinary number: it stays dark. Teammate has already sent two Remind messages as Ms. Rivera: "Bake sale next Friday! Cookie donations welcome." and a direct message "Can Anna stay for chess club Wednesday until 4:30?" | `/present`: "All quiet", the Do Not Disturb strip, the PhoneUse chip |
| 0:20 | "The school apps live on a cloud phone. Hushbell reads its notifications." | Nobody | Two rows slide in: LATER, and NEEDS YOU with its plan |
| 0:40 | "One needs a yes." Read the reply aloud. | Eli taps the approve button on the Hushbell app | The row says "Working on the phone…"; the console shows PhoneUse's agent opening Remind |
| 0:50 | "Now the thing Do Not Disturb really breaks. You're the school nurse: call this number." | The judge calls and reads the card | Blue banner "Hushbell answered a call", then an URGENT row: "Safety rule · before any AI" |
| 1:25 | (silence) | Eli's phone rings on Do Not Disturb; answer on speaker, near the mic | Yellow banner "Ringing Eli through Do Not Disturb · Press 1 to…" |
| 1:45 | "One key." | Eli presses 1. The judge's phone rings; Eli says "Hi, it's Eli, I'm on my way" | Green "Eli pressed 1"; the steps tick off |
| 2:15 | "And the teacher?" | Teammate holds up the teacher phone with the reply | The chess club row: Done |
| 2:35 | "Two messages and a call. My phone rang once. One key, one tap, and I never opened a school app." | Nobody | The four numbers |

**The three moments that must land**
1. **A judge's own phone makes a Do Not Disturb phone ring.** Real speech, real phone calls both
   ways; the safety rule fires on what the judge said.
2. **Press 1, and the judge's phone rings back.** The agent connects two people while the other
   steps tick off.
3. **The reply arrives on the teacher's phone**, typed by an agent on a phone Eli never touched.
   Approving at 0:40 lets that task finish during the call, so there's no dead air.

## Partner tools: shown, not listed

| Tool | Where the room sees or hears it |
|---|---|
| **PhoneUse** | 0:20, 0:40 and 2:15: the console phone at work, and the reply arriving |
| **LangGraph** | Press 1 resumes a paused run. Say it once: "the run was paused, waiting for me." |
| **ElevenLabs** | The voice on the speaker at 1:25. Say nothing about it. |
| **LangSmith** | Not in the 3 minutes. Keep a tab open on the nurse call's trace for questions. |
| **Tavily** | Not in the 3 minutes. Have "What time does the science museum open on the trip day?" ready for questions. |
| **Replit** | Only if it's deployed: "the laptop is just a screen." |

The big screen's header already lists the tools that are on, so there's no need to name them.

## Fixes made the night before

- Two characters on the demo messages showed as garbled text ("Room 12" with a stray "Â"). Fixed,
  and a test now stops it from coming back.
- A message with a safety word waited for Claude for up to 60 s before calling Eli. It now waits at
  most 8 s, then calls anyway (`HUSHBELL_URGENT_SORT_SECONDS`).
- The cloud phone is checked every 6 s instead of 20 s.
- With ElevenLabs on, the call's voice is made while the phone is ringing, so it speaks as soon as
  Eli answers.
- US carriers block texts from unregistered numbers, so in cloud mode a blocked text to Sam now
  becomes a short call to Sam with the same words.

## On the day: what to set up, in order

1. **Twilio: upgrade from trial before anything else** (10 min). A trial account can only call
   numbers you've verified and plays a notice first, which breaks connecting Eli to a judge.
2. **First real ring by 10:15.** ngrok on the hotspot, fill in `.env`, `hushbell doctor --live`, the
   Test call button. Then Rail A end to end with a teammate as the nurse.
3. **PhoneUse end to end (2–3 h).** Get a cloud phone, install Remind, sign in, add Eli to the
   teacher's class. Check that Remind's notifications arrive on the cloud phone, that Hushbell reads
   them (`doctor --live`), and that one reply task completes. **Time it**: if the reply takes more
   than about 90 s, approve earlier in the script.
4. **Keys** (10 min): LangSmith (`LANGSMITH_TRACING=true`), ElevenLabs, Tavily. Listen to one call
   with the ElevenLabs voice; if it sounds wrong or starts late, remove the key.
5. **Replit** (45–60 min), only if 1–4 are green by 2:30. Never run Replit and the laptop at the same
   time: whichever starts last takes over the Twilio number.

**Cut from the 3 minutes:** form filling (the demo link isn't a real form, and the agent can take
minutes), the Coming up list, installing the app, asking by voice, the WhatsApp merge, the USB
Android.

## Timeline

| When | What |
|---|---|
| 9:30 | Twilio: sign up, upgrade, buy a voice number. Star it on Eli's phone; allow it through Do Not Disturb. |
| 9:50 | Hotspot, ngrok, `.env`, `hushbell doctor --live`, Test call. |
| 10:15 | Rail A end to end. Try the text to Sam once. |
| 10:45 | Find the PhoneUse team: key, cloud phone, whether Remind's push arrives, whether there's a live view, and how long a task usually takes. |
| 11:00 | Rail B: install Remind, sign in, set up the teacher's class, first read, first reply task. |
| 1:00 | Lunch; add the LangSmith, ElevenLabs and Tavily keys. |
| 1:30 | Go/no-go on Rail B. |
| 2:30 | Replit, only if everything is green. |
| 3:15–4:45 | Three timed rehearsals with a stranger as the nurse. Record the backup video on the second clean run. |
| 5:00 | **Freeze:** no code changes, no ngrok restarts. |
| 10 min before | `hushbell reset`, clear the cloud phone's notifications, Test call, ringer up. |

## If something breaks

| Problem | Do this |
|---|---|
| The cloud phone is slow or dead | Press keys 1 and 4 on `/present`: real Claude still sorts them. Skip the 2:15 beat. |
| The PhoneUse task fails or stalls | Move on: "it only retries what didn't finish." |
| The judge's words are garbled in a noisy room | Hushbell asks "If this is an emergency, press 1"; the judge presses 1. If that fails too, the teammate calls. |
| Eli's phone doesn't ring | The red "Urgent · waiting for Eli" banner shows; approve on the app. "The call is the shortcut; the app is the fallback." |
| Hushbell doesn't answer the judge's call | ngrok was restarted: restart Hushbell so it repoints the number. |
| Claude is down | The safety rule still marks the fever urgent; other rows say "Claude unreachable · kept for you". |
| Wi-Fi | Use the hotspot from the start. |
| Everything is down | `hushbell run --no-phone --dry-run --demo`, drive keys 1–6 (0 starts over), approve the fever on the app. Then the backup video. |

## Questions judges will ask

- **"Why screenshot the shade?"** (the PhoneUse founder) "The screenshot needs no lease and isn't
  billed. We compare it to the last one, and Claude reads it only when it changes. A notification
  event stream from PhoneUse would replace this."
- **"What about sign-in or code screens?"** "The task stops in `needs_user_control`. Hushbell keeps
  the phone and tells Eli to finish it in your console, then press Try again. It never types a
  password."
- **"Prompt injection through a notification?"** "The reader and the sorter treat text as content.
  The phone agent gets a cleaned, quoted task, never the original message."
- **"What if it misses an emergency?"** "Fixed safety words run before any AI and lean toward
  calling. If Eli doesn't answer it calls once more, then keeps it at the top of the app."
- **"Does the school have to call a new number?"** "Today, yes, or Eli forwards unanswered calls to
  it. We haven't tested forwarding yet."
- **"What does it cost per family?"** "We haven't measured it yet." Don't guess a number.
- **"Who pays?"** "Parents, by subscription. Pricing isn't tested yet."
- **"Apps' terms?"** "It runs on Eli's own accounts, mostly reading, and replies only after he
  approves. WhatsApp is the strictest, so there it only reads."

## Don't

- Don't say a number or a competitor fact you can't source on the spot.
- Don't make the climax depend on the cloud phone or on form filling.
- Don't ask judges to type; one judge makes one call.
- Don't say "no server, it all runs on my laptop" while using the cloud phone or Replit.
- Don't say the room will see the shade being pulled down (it's opened once and stays open), that
  the calendar gets the event in cloud mode (it's a link to tap), or that Sam gets a text until
  you've seen one arrive.
- Don't name the tools one by one, and don't restart anything after 5:00.

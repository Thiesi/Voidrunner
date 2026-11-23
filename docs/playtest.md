# Playtest sheet

Every number in `docs/design.md` comes from tests or from simulated pilots.
Neither can say whether the game is fun, where a person gets lost, or when
they stop wanting to play. This sheet is for finding that out with two or
three people who have not seen the game, over three sittings each.

## Setting up

```sh
VOIDRUNNER_SAVE_DIR=/tmp/voidrunner-playtest python3 voidrunner.py
```

- One save directory per player, so each starts a new career.
- An 80x24 terminal for at least one player and a wide one (130 columns or
  more, 40 rows or more) for another. The game is laid out differently on each.
- Sit where you can see the screen. Say only: "It is a space trading game.
  Play it however you like, and say what you are thinking."
- Do not explain, point or answer questions about the game. A question the
  player asks is a finding: write it down, with the screen they were on.

## What to write down

One line per event, with the game day from the top of the deck.

| Day | Screen | What happened | Kind |
| --- | --- | --- | --- |
| 0 | Command Deck | Read the deck for 40 seconds, then pressed M | hesitated |
| 1 | Market | "Why is this one green?" | asked |

Kinds worth marking: **asked** (a question out loud), **hesitated** (more than
ten seconds without a key), **wrong key** (pressed something the screen did not
offer), **bounced** (left a screen within two seconds), **smiled** (anything
they visibly enjoyed), **sighed** (anything they visibly did not).

## Sitting one: the first twenty minutes

Stop after twenty minutes or the first ship upgrade, whichever is later.

- How long until the first jump? Until the first sale at a profit?
- Did they open the Guide? Did they take First Flight? Did they finish it?
- On the first arrival, did they read the results at the top of the deck?
- First fight: did they read the raider's pattern, or press Fire until it
  ended? Did they use Guard? What did they do at the prize page, and why?
- Did they ever look at the picture in the window, or only at the panel?
- Which deck entries did they never open?

## Sitting two: the next day

Let them start it themselves. Stop after thirty minutes.

- What did they remember from yesterday, and what did they have to relearn?
- Did they notice the daily dispatch? Did they try for it?
- Ask before they start: "What are you going to try to do today?" Then note
  whether the game let them do it.
- Did they lose a ship? What did they do next, and did they find dock work?
- When did they first say something like "I see how this works now"?

## Sitting three: a week in, or around day 40

- What are they working toward? Can they say what the next thing to buy is?
- Which screens do they open every time they dock, and in what order?
- Do they use the Ledger, the Viewport, the Archive, the Hall of Fame?
- When, if ever, do they say "what now?" Note the day.
- Do they talk about a raider, a crew member or a station by name?

## Five questions after each sitting

1. What were you trying to do just now?
2. What did you not understand?
3. What was the best moment?
4. Would you play again tomorrow? What would you do first?
5. One thing you would remove, and one thing you would add.

## What to bring back

- The event tables, one per player and sitting.
- The day each player reached their first upgrade, their first new hull and
  their first "what now?".
- Every question a player asked out loud, word for word.
- The answers to question 4. If the answer is no twice, that matters more
  than everything else on the sheet.

The simulator's milestone table (`python tools/career_sim.py --days 250`) says
when a plain pilot reaches each hull, rank and stake. Put the players' days
beside it: the gap between the two is how much a person's attention, rather
than the rules, sets the pace.

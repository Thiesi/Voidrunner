# The Regatta: the month's galaxy

Build plan item 5.3. This began as a proposal with three open decisions. They
were taken on 26 November 2026, and this is what was built.

| Decision | Taken |
| --- | --- |
| How a caller gets in | A second career beside their own |
| Which galaxy | The same on every board |
| What the table ranks | Net worth on the last day of the month |

## What it is for

Every career is flown in a galaxy of its own. Two callers on a board can
compare credits in the Hall of Fame, but not what they did: one had a rich
lane next to Freeport and the other started in the Long Dark. The daily
dispatch was the only thing every pilot shared.

The Regatta is one galaxy that every caller can fly in the same calendar
month, with a table of its own. Same stations, same markets, same sectors,
same seasons. What differs is the pilot, and the luck of encounters.

It came after pacing on purpose. With twelve jumps a real day everyone has the
same number of jumps to spend in a month, so the table measures what was done
with them. Without a limit it would measure hours, and on a board with no
limit the page says so.

## A second career

A caller flies the Regatta in a second career beside their own. Anybody can
enter in any month, and nobody gives up a career to do it.

- `[R]` on the deck, or `[R]` from the Hall of Fame, opens the Regatta's page.
- `[E]` on that page enters, after one question, and from inside it returns.
- The Regatta career starts as every career does: a Shuttle and a rookie's
  purse, at Freeport, in the month's galaxy.
- The caller's own career waits where it was left. Its file is not touched.
- A Regatta career's deck says which career it is on every deck, in a row of
  its own with the key that leads back.

A caller's own career is told once a month that the Regatta is open, from its
third day on. A new pilot has enough to read.

## One allowance of jumps

The jumps are the caller's, not a career's. Both careers draw on the one
daily allowance, and a jump flown in either is gone from both.

Each career file keeps its own record of the allowance: the day it was last
written and what was left then. Read later, a record says what would be left
if nothing had been flown since. The record that was written last is the true
one, and it is always the one that says fewer, so the allowance is the lesser
of the two. Neither career ever writes the other's file.

## The same galaxy on every board

A galaxy is made from one number, the career's seed: systems, links,
economies, market traits and seasons all follow from it. The Regatta's seed
follows from the month alone, as the dispatch follows from the day. Two boards
running Voidrunner fly the same galaxy in November, and nothing is configured.

A career's luck does not follow from the seed. Encounters draw from a
generator seeded by the system, so two pilots in one galaxy meet different
raiders.

## The table

The table lists the month's careers by net worth: credits, cargo at what it
cost, and what was put into the ship's modules, its hull and stakes. It is the
figure the career simulator has always compared careers by. A pilot who bought
an Argosy is not ranked below one who sat on the credits.

- Among equals, the career that got there in fewer days is ahead.
- The table shows the day each career has reached, because a caller who
  joined on the 20th has had fewer.
- A career's standing is taken at every save while its month lasts. When the
  month turns the last figure stands.
- Last month's first three stay on the page.

## When the month turns

A Regatta career ends with its month. It does not retire, and there is no New
Game+ from it. When the month turns, the next month's galaxy is a new career.

The month turns at midnight on the node's calendar. A caller who is flying
their Regatta career at that moment is returned to their own career at the
next deck, with a line that says the table is final.

## Where it is kept

A Regatta career is an ordinary career file. It is kept in a directory of its
own under the save directory, with its own score records beside it:

    saves/
      77.json                      the caller's own career
      scores/77.json
      regatta/
        2026-11/
          77.json                  the same caller's November career
          scores/77.json           and its standing

The storage layer is asked for a different directory and is otherwise
untouched: checkpoints, the previous copy, recovery and the score record all
work as they do for any career. The session holds the caller's one lock, which
is in the save directory itself, for both.

A save gains one field, `regatta`, the month, written only for a Regatta
career. A score record gains `regatta`: the month, the net worth and the day.
Every save and record written before this reads as it did.

**For a board's backups:** the save directory now has a subtree. A backup that
copies only the top level will miss Regatta careers.

## SysOp settings

- `DOOR_REGATTA=0` switches the Regatta off. The door then has no such page,
  and a caller who was inside one is returned to their own career.
- `DOOR_REGATTA=2026-11` pins the month. The gallery uses it; a board has no
  use for it.

## What it does not do

- **No shared state.** Pilots do not meet, and one pilot's trading does not
  move another's prices. Markets are each career's own.
- **No prizes and no achievement.** A table, and last month's first three.
- **Achievements, the dispatch streak and the wire are each career's own.**
  What a Regatta career earns is recorded in that career and shown in its own
  Hall of Fame, not added to the caller's own Pilot Record.
- **No hard charter.** Every Regatta career is on the standard charter, so
  the table compares like with like.
- **NetBBS game events are sent for a caller's own career only.**

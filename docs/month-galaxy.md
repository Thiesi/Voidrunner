# The month's galaxy: a design note

Build plan item 5.3. This is a proposal, not a description of anything built.
It asks for three decisions, each with a recommendation.

## What it is for

Every career is flown in a galaxy of its own. Two callers on a board can
compare credits in the Hall of Fame, but not what they did: one had a rich
lane next to Freeport and the other started in the Long Dark. The daily
dispatch is the only thing every pilot shares.

The month's galaxy is one galaxy that every caller can fly in the same
calendar month, with a table of its own. Same stations, same markets, same
sectors, same seasons. What differs is the pilot, and the luck of encounters.

It was put after pacing on purpose. With twelve jumps a real day everyone has
the same number of jumps to spend in a month, so the table measures what was
done with them. Without a limit it would measure hours.

## What is already there

Most of it. A galaxy is made from one number, the career's seed
(`SaveData.seed`): systems, links, economies, market traits and seasons all
follow from it. A career's luck does not: encounters draw from an event
generator that is seeded by the system, so two careers on one seed meet
different raiders.

So a shared galaxy needs no new world code. It needs a seed that everyone
gets, a way to choose it, something to remember that a career is in it, and a
table.

## Decision 1: how a caller gets in

**A. At launch, like the hard charter (recommended).** The launch question
gains a key: `[M]` for the month's galaxy. A pilot who retires can choose it
for the next career on the Career Finale page. The career is an ordinary
career in every way; its seed is the month's.

- Smallest change. It reuses the pattern #115 built for the hard charter, and
  the two can be combined: a hard career in the month's galaxy.
- One career per caller, as now.
- A caller in the middle of a long career cannot join until that career
  retires. A new caller can join at once.

**B. A second career beside the first.** Every caller has their own career
and, separately, a career in the month's galaxy, and chooses which to fly at
launch.

- Everyone can join in any month, which is the point of a monthly event.
- It is a large change. The door, the save layer, recovery, the backup
  import NetBBS uses and the score records all assume one career per caller.
- The flight allowance is the caller's, so the two careers would share
  twelve jumps a day. That is right, but it means the monthly career competes
  with the caller's own for jumps.

**C. A, and a leave of absence later.** Ship A now. If callers ask to join
mid-career, add a way to park the current career and come back to it, which
is B's storage work without B's menu.

Recommendation: **A now, C if it is wanted.** B is the better event and the
riskier change, and A will show whether anyone flies it.

## Decision 2: one galaxy for every board, or one each

**The same on every board (recommended).** The seed follows from the month
alone, as the dispatch follows from the day. Two boards running Voidrunner
are flying the same galaxy in November, and a caller on both can compare.
Nothing is configured.

**One per board.** The seed also takes something of the board's. Boards
cannot compare; a caller cannot look the month's galaxy up from another
board. I see no advantage unless SysOps want it.

A SysOp switch turns the month's galaxy off, as `DOOR_DISPATCH=0` does for
the dispatch.

## Decision 3: what the table ranks

The table lists careers launched in this month's galaxy.

**Net worth on the last day of the month (recommended).** Credits, cargo at
cost, and what was put into the ship, the crew and stakes, as the career
simulator already counts it. A pilot who bought an Argosy is not ranked below
one who sat on credits. The figure is taken at each save while the month
lasts and is final when the month turns.

**Credits.** Simpler, and it is what the Wealth table shows. It punishes
spending, which is the wrong lesson.

**Day reached, or contracts, or systems charted.** Each rewards one way of
playing. A later version could show all of them as columns.

Whatever is ranked, the table should say how many jumps the career has flown,
because a caller who joined on the 20th has had fewer.

## What a career would record

- `league`: the month it was launched in, `2026-11`. Absent for every other
  career, so every save written before this reads as it did.
- `league_worth`: the net worth at the last save made inside that month.

A retired career's dossier and the board's score record would carry both, as
they carry `charter`. A career goes on after its month ends; it is an
ordinary career, and only its place on that month's table is fixed.

## What it would not do

- **No shared state.** Pilots do not meet, and one pilot's trading does not
  move another's prices. Markets are each career's own. That would be a
  different game, and the door's one-file-per-caller storage is not built
  for it.
- **No prizes.** A table and, I would suggest, one achievement for finishing
  a month in the top three with at least three pilots on the table.
- **No change for a board with one caller.** They would see a table with
  their own name on it. The month's galaxy is still the same galaxy next
  door.

## Where a board has no jump limit

With `DOOR_JUMPS=0` a caller can fly as many jumps as they like, and the table
becomes a measure of time spent. The table should say so on such a board. I
would not hide it.

## Size and risk

With decisions A, same-on-every-board and net worth: a medium change, about
the size of the hard charter (#115). New save field, one more key at launch
and on the finale page, one more Hall of Fame table, the SysOp switch, and a
net worth function in the door that matches the simulator's.

The risk is in the calendar. A month turns at midnight on the node's clock;
a career launched at 23:59 on the last day is in the old month. The dispatch
already solved the same problems for the day, and this would use its clock.

## Questions for you

1. A, B or C for joining?
2. The same galaxy on every board, or one per board?
3. Net worth, or credits?

# Voidrunner design

This is Voidrunner's normative game design: rules, screens, saves and
presentation. It was imported from NetBBS's design document
(`docs/NetBBS-design-doc.md` at Thiesi/NetBBS@61de9c2c) when the game moved to
this repository (Thiesi/NetBBS#1214), and is maintained here from now on.
NetBBS's own document keeps only how NetBBS hosts the door.

Read the imported text with three conventions in mind:

- **Issue numbers** such as "issue #421" refer to Thiesi/NetBBS, where the work
  was done. Issues in this repository are written `Thiesi/Voidrunner#N` until
  the imported numbers are gone.
- **Section numbers and guides** ("section 13.4", "the door guide") are NetBBS's
  documents.
- **Paths** under `src/netbbs/` and `scripts/` are NetBBS's; here the door is
  `voidrunner.py` and the tools live in `tools/`.

## Game rules, careers and screens

Voidrunner saves retire their legacy paths at schema version 2 (issue #421).

The save validator rejects unknown fields and neither `schema_version` nor
`galaxy_version` has ever moved, so "additive only" has been the sole
compatibility strategy: every feature added since launch carries a runtime branch
for careers that predate it, spread through domain and UI code rather than
isolated behind a migration. There are eight such shapes -- the pre-tactics fight
rules and the `tactics is None` forks, legacy futures contracts and their price
multiplier, cargo with no recorded acquisition cost and the `uncosted_*` ledger
fields, duplicate mission ids, sequential squadrons without a `formation` record,
and the pre-`scores/` `leaderboard.json`.

There is no migration. `SCHEMA_VERSION` becomes 2, a schema-1 career is refused
with a message that says the save predates this version and a new career can be
started, and the eight runtime branches are deleted so the validator can require
the new shapes. This is a door game whose affected population is a handful of
pre-overhaul careers on a handful of nodes; a migration for them would be more
code, and more code that can go wrong on someone's save, than the thing it
protects. Hall of Fame records are the exception worth keeping, because they
outlive careers by design, including for pilots who never launch again: a
`leaderboard.json` predating `scores/` is imported in full, every row of it, and
the old file is retained. Because that writes files other sessions own, the
import runs under the maintenance gate -- the same exclusion a restore takes, and
the one thing that guarantees no pilot session is aboard -- and a launch that
cannot take the gate skips the import rather than racing a live checkpoint. It
belongs to the launch, not to a career: it runs before the career is loaded and
whatever the load then decides, so a node whose only returning caller is refused
still imports its rankings. It is complete-or-retry: attempted on each launch
until every row has been written, so neither a busy node nor a transient error
strands a historical ranking.

The refusal is a first-class outcome, not an error: it names the reason, changes
nothing by itself, and offers to begin a new career. Declining leaves the file
exactly as it was, so a caller who wants to fetch the old build first can. There
is one active save slot per pilot, so accepting the offer takes it: the schema-1
document is retained as a recovery copy and the new career is written in its
place. Not `.previous`, as first planned -- `.previous` is the *preceding
checkpoint*, so the launch tick that follows registration would overwrite it and
the retention would have been a promise good for one turn. The recovery namespace
is the one that is never removed, and is already where a rollback puts the career
it replaces. Failing to archive fails the replacement, because taking the slot is
not worth losing what was in it. That is a deliberate replacement of a
readable-but-unsupported career, not the case `write_save` refuses, which is
overwriting a career it cannot read at all.

Shipped as a single slice: `SCHEMA_VERSION = 2`, the refusal path, and the
deletion of the branches it makes unreachable, together, because a half-retired
legacy path is worse than either end state. What the validator now requires,
rather than tolerating and repairing, is the list itself: every fight carries its
tactical state and the hull it started the exchange with, every futures order
names its station, its goods cost and the stock it reserved, every unit in the
hold has an acquisition-cost lot, active contract ids are unique, a two-raider
encounter carries its formation record, an economy event names the stations it
reaches, a career records the contraband milestone step it was awarded under, and
the four fields a pre-overhaul career could omit are present. The repairs those
shapes needed are gone with them: no per-turn id renumbering, no `uncosted_*`
ledger totals, no second set of fire and evasion rules, and no leaderboard read
on the commit path. The id counter's own maintenance stays, because the counter
is derived state rather than a saved invariant, and a posted offer keeps its id
after acceptance, so uniqueness is a property of the active contracts and not of
the board.

Splitting the module into a package
(`domain.py`, `save.py`, `ui/`) is a follow-up to that slice; the launcher
resolves the door through `resolve_bundled_door_path`, which would need a package
entry, and the tests import the module by path.

Voidrunner's showcase development is tracked in issue #310. Completed station
actions (including trading, equipment, crew, contracts, scans, faction rewards,
landmarks and retirement) are committed before their success acknowledgement;
leaving a nested menu is not a prerequisite for saving. Each completed
route hop commits before another departure can be chosen. A failed checkpoint
stops play and retains an explicit error instead of accepting further actions.
Cancelling initial career launch creates no new career, invalid customs keys
make no changes, and the shipyard's displayed upgrade letters work directly.
Interrupted journeys resume before station access. Departure costs and turn
advancement, encounter selection, opponent damage, combat outcomes, mission
payouts, docking, and customs decisions commit with their corresponding progress
state before narration or further input. A restart preserves the event RNG state
and cannot reroll an encounter or duplicate a reward. Only the interrupted hop
resumes; the pilot can plan the remaining route after resolving it. Existing
galaxy seeds stay compatible; schema-1 careers are refused at load rather than
carried forward, as described above.
Career loading validates the supported schema and galaxy-generator version,
record shapes, types, references and gameplay ranges before any startup write.
Missing additive fields retain their legacy defaults; unknown structural fields
or unsupported versions stop play rather than being silently stripped. Galaxy
generation remains version 1 with its original seed/RNG call sequence.

Unreadable or invalid careers enter a read-only recovery screen; they are never
renamed or replaced by an automatic new career. Each changed checkpoint retains
the preceding readable file at `<user_id>.previous.json` before replacing the
current save. Rewriting an identical checkpoint does not age this copy. If the
current file is missing but a previous copy exists, recovery is still required.
The screen shows the candidate's callsign, day, credits and whether travel is
pending. Restoring it explicitly rolls back progress to that checkpoint, requires
a final confirmation, and first retains the original bytes in a unique recovery
copy. Back/EOF writes nothing. Unreadable or oversized originals, invalid previous
copies, versions this build cannot recognise and failed preservation require
manual SysOp repair; there is no caller-facing reset that bypasses preservation.
A career from a *known older* schema is the one exception, because nothing about
it is in doubt: it is offered a new career instead, and accepting still preserves
the old document, as a recovery copy, rather than bypassing preservation.
Recovery copies are limited to eight per pilot; a SysOp must archive older copies
manually before another restore, and a refused career is not replaced at all once
that limit is reached. The pilot session lease covers recovery as well as normal play.
This local previous-checkpoint copy is not a substitute for node backup coverage.

The optional Pilot Guide is reachable from the station deck without changing
state. A new pilot still docked at Freeport on day zero may accept one introductory
delivery to an actual connected station. Its legal cargo follows that station's
demand; the quote includes procurement, a return fuel reserve, crew wages, gross
payment and destination danger. No galaxy RNG calls or generated systems change.
Acceptance is explicit after paginated terms, consumes an ordinary active-contract
slot and tracks the objective. The job has no deadline, uses normal market and
delivery rules, and cannot be taken again after completion or abandonment in the
same career. Its payment covers the quoted cargo and round-trip fuel/wages plus
200 credits; changing prices, detours and encounters can change the result.
The guide explains buying cargo, jumps advancing game time, refuelling, first
upgrades and encounter choices, and retains a recap of active commitments for
returning pilots. Reading or declining it neither writes a save nor advances time.
Accepting First Flight returns straight to the station deck, which shows the next
step (market, quantity, chart destination) as a result line (issue #415).

What the game has to say before its first screen is part of that screen, never a
line written ahead of it: every screen begins by clearing the terminal, so a line
printed first is erased before it can be read (issue #641). The first Command
Deck of a session therefore opens with the pointer to the Pilot Guide for a new
pilot, or with a welcome and the recap of commitments for a returning one, shown
once in the place results are shown. A pilot whose last session ended
mid-journey is told so on a screen that waits for a key, since the encounter's
own first panel follows it. A new career after retirement, and a career rolled
back to its previous checkpoint by the recovery screen, announce themselves on
their first deck the same way.

Escape answers No at every yes/no confirmation and cancels a quantity field,
erasing what was typed (issue #413); prompts say so. Unsupported keys still do
nothing at either prompt.

Voidrunner uses one terminal-key decoder for menus and text fields, with UTF-8
or CP437 text decoding according to the active display encoding.
Unsupported special keys, terminal control strings, and bracketed paste cannot
become menu commands or confirmations. Partial sequences survive transport gaps;
a lone Escape is harmless and does not block indefinitely. CRLF submits a field
once. Callsign editing supports combining accents and wide characters, bounds
both character count and display columns, and normalizes submitted text to NFC.
Quantity fields accept ASCII decimal digits. Unsupported keys do not dismiss
result acknowledgement pauses. Responsive layouts and broader retained-result
presentation remain separate work in #310.

DOOR32 comm type 2 selects the inherited socket as the terminal transport on
Windows and POSIX. Telnet control sequences are discarded before the shared key
decoder sees them; escaped IAC is data, and CR-NUL and CR-LF submit once. Output
is encoded first, then literal IAC bytes are doubled. Option negotiation stays
with the host. The door closes only its own inherited handle on return and does
not shutdown the connection or change its blocking mode. Socket EOF and reset
use the existing career checkpoint path, including when output detects the
disconnect. A storage failure remains an error even if its diagnostic cannot
reach the disconnected caller. Stdio remains the default for other launches.

Known host allowances use a monotonic deadline measured from door launch:
NetBBS supplies `session_limit_seconds`, classic drop files supply minutes.
Every page shows remaining time in its bottom border, refreshed when drawn,
so pagination cannot hide it. At three minutes a single top-row warning
preserves the current prompt and partially typed input. Idle reads, partial
keys, resize waits, storage-lock waits and the splash hold still honor the deadline. The door
reserves the smaller of one second or ten percent of the allowance, commits
the career and then reports its return to the BBS. A failed save stays an
error and is never retried by the expiry path. Missing or invalid allowances
do not bound play; zero expires immediately. Time limits are session metadata,
never save data, and resizing does not restart the clock.
Expiry is distinct from a disconnected input stream, so a caller waiting at an
outdated-career or recovery prompt also receives the graceful return message
without replacing the existing save.

Voidrunner's Back key is `B` on every screen below the station deck, and `B` is
never a live action anywhere (issue #400). List screens allocate their selection
letters from `choice_letters`, which skips `B` and the screen's own hotkeys;
bribes are `P`, the combat information toggle is `I`, and derelict boarding is
`S`. `Q` stays accepted as a quiet alias for Back where no action uses it; on
the deck `Q` saves and disembarks, and the deck's `B` opens the read-only Mission
Board. Choosing a connection on the direct chart is the one irreversible chart
action, so it ends with a yes/no confirmation that states the fuel cost, the
destination's danger (or unknown) and that a day passes; No retains the chart
with a cancelled-departure result and writes nothing. Contract and route jumps
keep their existing preview screens and deliberate Jump actions.

Every hotkey the game prints is written `[K] Label` -- one style, everywhere
(issue #400). The game used to mix three: `[B]ack`, `[B]Back` and `[B] Back`,
sometimes two of them in one action bar. Of the two styles the report named,
`[B]ack` is the one that cannot be carried through, because a hotkey is not
always the label's first letter and is not always one letter: `[1-9] Details`,
`[<>] Page`, `[M] Commodity Market` and `[X] Expand` have no inline spelling.
So `[K] Label` it is, and it was already what every dynamic list row and every
combat detail line used. The bar's label is the word the detail line leads with,
so no action is named twice; that is what renamed the combat verbs Brace to
Guard and Bribe to Pay bribe, and it is why Guard rather than Brace, since `B`
is Back everywhere and a stray Back must never spend a combat turn. The saved
field stays `brace_ready`. The style costs a narrow terminal a row: at the 40x12
floor a bar of five hotkeys wraps to two rows where the glued spelling took one,
so a page holds one row less and the wrap can fall between a key and its label.
That is paid for by the paginator, which measures the bar it will actually show,
and it is worth one row not to make the player learn two spellings of the same
thing.

At every action bar -- service pages, the chart, the mission board, the star
map, the route planner and the draft editors alike -- whitespace and unsupported
terminal keys are absorbed at the prompt rather than reprinting the page; an
unknown hotkey still redraws so the action bar is shown again (issue #416).
`read_command_at_prompt` is the single reader for a command key, so the rule
cannot hold on some screens and not others. No screen may therefore treat whitespace
as a hotkey: the Pilot Record and Hall of Fame previously left on an unlisted
Space, which both cost a redraw for every stray keypress and contradicted `B`
being Back on every screen. Counts in prose are pluralised and the board says
"New offers on day N".

Every paged Voidrunner screen shares one paginator (issue #418). A screen names
its content as groups -- a wrapped line, an authored silhouette, a keyed list
entry -- and the paginator keeps a group whole where it fits, continues it on the
next page where it does not, and never repeats a selection letter on one page.
Prose screens are the degenerate case: every row is its own group. Capacity is
measured once, from the wrapped title and the action bar the caller will see.
Page movement is clamped in one place, for both spellings of the paging keys
(`<`/`>` and `P`/`N`), and a screen pages with the pair it advertises, because
`N` is a live action on screens that do not page with it. A screen that measures
or clamps for itself will disagree with the rest at some terminal width, so it
must not.

The loop itself stays with each screen. A single driver owning Back, paging and
dispatch for every paged screen was considered and rejected: it would rewrite the
control flow of some thirty screens in one change, against a test suite that is
their only safety net, to remove one line per screen that the shared page step
already removes. The paginator is the part that was genuinely duplicated.

Route planning starts from something real (issue #415): the chart's route
planner opens its destination picker at once instead of an empty screen, the
ledger lists Opportunities first, and the ledger's route draft pre-fills from the
best remembered lead when one exists. Counts in prose are pluralised.

Contract details state only the notes that apply (issue #412): the cargo-basis
note when cargo of that commodity is already aboard, the crew-wage note when crew
is aboard, the survey note for survey contracts whose target is still uncharted,
and the unknown-danger note when a leg is uncharted; the general reminders live
in the Pilot Guide. A blocked survey says only that revisiting cannot complete
it, because scanning cannot complete it either. Every offer's earlier pages say
where Accept is instead of hiding it, First Flight included. One-page screens
drop Prev/Next from their action bar but keep the page counter, and a screen is
paginated against the bar it will actually show, so dropping the tokens can be
what makes it a single page. Market rows carry no filler status word, the
exchange reminder appears once in the catalog, and a row that would overflow the
terminal gives up its depth figures first and its hold count only as a last
resort -- never a truncated word -- so a tagged contraband row still reads as
one row, and a terminal too narrow for even the shortest form keeps the whole
row and wraps it.

A hop's narration is retained for the next deck page (issue #410): the departure
charge itself -- fuel burned, fuel left, crew wages paid and credits on hand --
then events, discoveries, encounter and combat outcomes, customs results,
settlements and mission completions appear as `Result:` lines on the first
Command Deck page after the jump, and are cleared by the next deck action. The
report is session state, never persisted and bounded to the newest eight lines
so the deck still fits 24 rows. A journey resumed after an interruption
therefore reports the phases it actually replays, not the ones that completed
before the interruption: narration is a convenience for the caller who is
sitting there, and is not worth a save field or a resume-consistency rule of
its own.

Posted deliveries scale with the pilot's hold (issue #408): a board draws the
quantity between three and the larger of ten and two fifths of cargo capacity,
so a Shuttle's boards keep their existing range while a Carrier sees bulk
contracts whose reward scales with the quantity. Spot-stock pools do not limit
contract cargo, which gives large hulls work that depth cannot throttle. A
board is therefore reproducible from its seed, station, day and the pilot's
hold rather than the first three alone; posted boards are persisted and are
never regenerated under a pilot, so offers already on a board never change.

Completed legal contracts (deliveries, surveys and escorts) earn one point of
Concord standing each (issue #407), so a contract-running career reaches the
commission without combat; lost or expired contracts earn nothing. Combat and
story rewards are unchanged. A bounty pays its Concord standing through the
kill, not the contract. The award is stated where the player meets it: contract
terms name it before acceptance, the completion message and pilot log name it
when it is paid, and each faction contact screen names how its own standing is
earned.

Contract boards retain up to four posted offers per station for three game days
from generation. Station checkpoints prepare offers and expire old jobs before
the caller opens the board; browsing and backing out write nothing. Viewing or
accepting offers does not replenish them; jumps
advance game time. Posted terms and consumed offers survive restart. New careers
may hold at most three active contracts; legacy over-limit careers retain every
job but cannot accept more until below the limit. IDs are unique across active
jobs and cached offers, and the validator requires it: a document that repeats one
is refused rather than repaired.
Deadlines are inclusive: a contract is eligible on its deadline day, expires
before rewards or mission encounters on the next day, and is never paid late.
Already-charted survey offers are unavailable. Selecting a posted or active
contract opens its complete terms without writing. Details include the named
target and coordinates (without charting it), minimum hops and first bearing,
inclusive deadline, cargo procurement and hold needs, gross payout, and estimated
remaining cash outlay at current prices. Estimates explicitly exclude sunk cargo
cost, repairs, detours and market changes. Escorts warn of a fight on every jump,
including detours and other concurrent escort jobs. Survey contracts use a
SURVEY label and explain that arrival or a scanner discovery can complete them.

Acceptance is a separate action on the final details page. One active contract
may be tracked, persisted across sessions and shown at the station and chart.
Tracking reveals its bearing, not remote market or danger information. Abandonment
requires a final confirmation, forfeits the reward with no extra fee, retains
cargo, and does not repost the offer. Completion, expiry and abandonment clear
tracking. Contract lists and detail text paginate to terminal dimensions, with
Back on every page. Broader map/route planning remains in issue #310.

Contract details offer a read-only Route screen, including for posted jobs before
acceptance. It plots every connection to the named target, while uncharted
intermediate names, economy and danger remain hidden. The contract target's name
and coordinates are public terms, not a chart discovery. Active routes account
for queued bounty re-entry, fuel, wages, refuelling stops and inclusive deadlines.
The chart also opens the currently tracked contract route directly.

Jump next is available only for a current, unexpired active contract while docked
and with fuel for that leg. A survey target must still be uncharted: discovery
retained from a destroyed journey cannot be repeated for a reward, so its contract
is explicitly blocked and can be abandoned. It tracks the selected contract and checkpoints that
intent, then uses ordinary durable travel for exactly one jump. The pilot chooses
again after each outcome; diversion, completion, failure, expiry, fuel or crew
changes cause a fresh route/budget. No unattended remainder is resumed after a
restart. Browsing, Back and EOF do not track, accept or travel. At the destination,
delivery shortages are explained; bounty jobs still require ordinary re-entry
rather than a fabricated in-place encounter. General named-destination routing follows the same one-hop control described below.

General navigation opens a screen before asking for a destination. Its paginated
picker lists charted system names; uncharted contract targets remain available
through their contract route. A selection is a temporary route preview and writes
nothing. Back, destination cancellation and EOF leave the career unchanged.
The preview shows every leg, unknown threat labels, fuel, wages, manual refuelling
stops and active-contract deadline estimates. Jump next validates and flies one
ordinary durable leg, then retains the result and recalculates. Refuelling does
not need to fit the whole journey into one tank. Choosing a new destination or
leaving the screen interrupts the plan; selection must be made again after Back
or restart. No multi-hop execution or yes/no dialogue remains in this screen.

The spatial star map is a read-only view of the existing seeded coordinates and
connections; it does not change galaxy generation or save state. It opens in the
current sector with next/previous sector navigation and a galaxy overview. Known
stations, the current position, tracked objective and an explicitly plotted route
have distinct ASCII markers, with a paginated list alternative and exact-coordinate
station inspection. The list is always available; the spatial map needs the room
the 40x12 floor guarantees.
Projected cell collisions are labelled; the exact list/inspection remains authoritative.
Only discovered station details are public, except a contract's named target.
Uncharted route points show bearings and unknown danger. Ordinary connection lines
join charted endpoints; the plotted path can cross uncharted space without revealing
other links there. Contract/general navigation opens the map with its current path.
Map Back returns to the same route; inspecting never charts, tracks or travels.

Engineering-yard and crew screens paginate by wrapped rows and negotiated height.
Narrow screens stack upgrade/crew terms instead of wrapping padded table borders.
Keep each label, price and benefit together when the entry fits a fresh page.
Credits appear in the page heading; fuel, hull, hire costs, ongoing wages and refit
terms remain available in full. Direct action letters keep their meanings across
pages, with previous/next punctuation keys and Back always present. Completed or
rejected actions retain their result at the start of the refreshed screen. Back,
paging and cancelled action prompts do not checkpoint or spend. Confirmation is
only behind a chosen purchase/dismissal action, and existing durable action
checkpoints still precede success acknowledgement.

The direct-jump chart uses height-aware connection entries rather than a fixed
79-column table. Fuel stays in the heading; each connection retains its bearing,
known sector/economy, danger or unknown label, jump cost and tracked-next marker.
Entries remain together when they fit a fresh page, and overflow can always be
browsed with previous/next controls and Back. Standard connection keys stay stable;
exceptionally dense charts reuse the reserved-key-safe alphabet only on later
pages. Rejected departures and scan results remain visible on the refreshed chart.
Browsing does not change the career; scans and travel retain their existing rules.

The station command deck uses the same bounded paragraph pagination as service
screens. Credits and Exit remain in every page's controls; service hotkeys keep
their meaning even when their labels are on another page. Compact telemetry uses
explicit hull/fuel fractions and cargo used/capacity, without treating a full hold
as healthy or an empty hold as damage. Contraband and economy-event warnings can
appear together; low fuel, critical hull, unaffordable wages and the tracked
objective provide contextual next actions. Expand adds pilot, location, commitment
and achievement details for this visit; it is a read-only view toggle, not a saved
career option. Paging and invalid keys do not repeat station settlement. Completed
settlements retain their result in the deck after the existing durable checkpoint.

Pilot records provide direct Overview, Contracts and History views with Back and
pagination on every page. Contracts retain complete descriptions, public target
coordinates, inclusive deadlines and reward context; the mission board remains
the place for contract actions. History exposes every already-retained highlight
and log entry newest first, without extending storage retention or silently hiding
earlier entries behind a display limit. Read-only pages are cached per view for
the visit so large legacy records are not reformatted on every key. Retirement
uses the Finale screen, final chosen-action confirmation, new-career
bonus and durable checkpoint. Cancelling retains the record with a visible result.

Pilot rank is lasting recognition within the current career. Each completed-action
checkpoint records any crossed balance threshold in the existing highest-rank
field, with a promotion highlight and log entry, before another menu action can
spend that balance. Displays and retirement eligibility use the greater of this
retained rank and the current balance rank. Purchases, fines and salvage cannot
demote the pilot. Rank terms show the next balance threshold and distinguish it
from current funds; a legacy promotion is retained even when credits are lower.
A lifetime leaderboard high-water mark must never promote a new career. Retirement
resets rank as before; subsequent starting-credit bonuses are ordinary current
funds. Old careers without a recorded promotion can establish only what their
current balance proves. Read-only rank views do not write or fabricate history.
Promotion notices do not introduce a separate acknowledgement dialog.

The pilot record exposes three independent accomplishment paths as well as the
balance-based rank. Trader stages use recorded known-cost market-sale margin at
5,000/20,000/50,000 credits; delivery pay, unknown-cost receipts and non-trading
income do not count. This is cargo margin, not total career profit. Explorer
stages require 12/30/all 48 charted systems. Combat stages require 5/20/50 recorded
victories. Progress is factual and may fall where its underlying measure falls;
current-career rank remains permanent. These paths unlock different career
conclusions without requiring the 150,000-credit top rank. That threshold is set
so the last rank does not become an empty stretch: a greedy trading career
finishes every ship purchase and the trader finale by roughly day 45 and reaches
Void Baron near day 84, and the remaining climb to Legend must not be scores of
identical jumps with nothing left to buy. A wealth-scaled sink or goal for the
late game remains open work, and human playtesting decides whether one is
needed. Earned ranks are retained, so lowering the bar only ever promotes.

The always-available record Finale screen shows all four conclusions and their
requirements before choosing: Frontier Legend at retained top rank, Trade Guild
Founder at 50,000 recorded market margin, Atlas Keeper with a complete atlas, or
Frontier Warden after 50 victories. Selection is a read-only draft. Only the
chosen Retire action followed by final confirmation ends the career. It records
a distinct closing account and begins a fresh galaxy, retaining the existing
500-credit-per-retirement bonus. The specialist conclusions also start the next
Shuttle with one ordinary cargo/scanner/weapon tier respectively; the Legend
conclusion keeps the ordinary starting ship. No other gameplay progress carries
over. Thus the chosen conclusion determines a concrete starting advantage.

Retirement retains a compact dossier in the same atomic career checkpoint as the
new run: sequence number, original seed/dates, conclusion, rank, ship, days,
credits, chart count, victories, missions, known market margin and retained
highlights. A confirmed retirement ends on a held Career Complete screen (issue
#644), drawn only after that checkpoint is on disk: the ending's name and
closing text, the portrait of the ship that flew the career, the career in
numbers and its last highlights as the archived dossier records them, and what
the next pilot starts with. Any key but a paging key leaves it, Enter, Space and
Escape included. The record's Dossiers view includes every retained dossier. Old
retirement counts do not fabricate missing dossiers. Storage permits 128 dossiers
and refuses further retirement visibly at capacity; it never evicts an older
record. Existing careers remain playable at the cap. The Retire action preflights
the normalized replacement against the existing save-size/schema envelope before
final confirmation; insufficient space retains the current career with an explicit
unavailability result. Accepted legacy highlights are preserved, not clipped to
the live append helper's retention limit. Version checks precede v1 field validation,
with unsupported/new fields preserved for the appropriate build.
The lifetime score remains separate from current-run rank and achievements.

Station Viewport and yard ship portraits expose distinct native terminal artwork
for each of the four hull classes, five station economies and four discovered
landmarks. Damage changes the hull's visual shading while explicit hull/fuel/cargo
figures remain authoritative. Portraits use full and compact authored compositions,
retaining an entire silhouette on one page at supported terminal dimensions; text
and navigation paginate normally. Existing full/basic/mono/plain modes apply.
The plain mode uses ASCII art without color. A portrait is a still composition --
it may be revealed under the motion rules in the presentation contract, but it is
never redrawn -- and viewing one makes no random draw, market observation or
career write. Station captions use actual
places, sectors and specialist contacts. A landmark portrait is available only at
its station or after investigation, not merely through an accepted bearing.
Ship commissioning previews and landmark inspection use these same portraits;
backing out of either view leaves gameplay unchanged.

In every preset with color (`auto`, `fast`, `basic`) the Viewport itself is a
picture rather than a portrait, at the request of the first external SysOp: the
in-flight scenes' half-block pixels, filling the terminal above a caption that
carries every fact the portrait's text did. The ship is the pilot's hull, lit
along its plating, its drive burning, the world it is docked at behind it;
damage is scorch marks, then sparks, then smoke and a cockpit alarm. The station
is the docking scene's, close and still, with traffic on its approach. Each of
the four landmarks is drawn as itself, and with none recorded the scanner sweeps
empty space. A picture opens its blast shutters and then idles -- a ring turns,
a beacon blinks -- until a key, which is the command the action bar asked for,
not a skip. The idle stops after about twenty seconds so an idle caller is not
sent a picture forever; `fast`, `DOOR_SCENES=0` and a session with no live
terminal get the still frame. `mono` and `plain` keep the portraits, and so does
a terminal too small to hold a picture over its caption (a landmark's story at
40x12). Opening and the whole idle send under 100 KB at 80x24 in truecolor.

Hall of Fame offers five separate local achievement views: lifetime wealth,
known-cost market margin, exploration, combat victories and completed careers.
Wealth and completion counts rank pilots; trading, exploration and combat rank
individual current or archived careers. Each view shows at most 20 entries with
stable numeric pilot/career tie-breaking. Full callsigns, values, career number,
seed and current/completed state remain available on paginated narrow screens.
There is no combined score. Market margin has the same exclusions as the trader
career path; combat counts the existing generic victory counter, not raiders only.
Valid legacy/per-pilot lifetime retirement maxima are imported into the career
save before projection, preserving current career numbering and future retirement
bonuses without inventing old dossiers or changing current funds. Legacy wealth
and retirement counts remain valid, but absent career metrics are
labelled unavailable rather than fabricated. The five views share one read-only
snapshot per visit. Switching categories, paging and Back never write scores.

Each pilot's optional score projection contains bounded versioned summaries of
all retained retirement dossiers plus the current run. A checkpoint regenerates
these summaries from the authoritative career save, including after an optional
score-write failure across retirement. No top-20 filter discards stored history.
Legacy retirement gaps remain gaps, and the existing 128-dossier capacity applies;
no separate archive files or gameplay counters are added. Future score-summary
versions are left untouched by older code. Dossiers retain the fuller personal
history; shared score summaries omit their highlights.

Shared-seed competition is deliberately deferred. Existing careers have different
New Game+ advantages and rule histories, so matching seeds alone would imply an
unsupported fairness guarantee. These are local accomplishments, not certified
competitive runs. A future challenge would need its own versioned rules and equal
starting conditions without changing the single-player career core.

The spot-market catalog uses height-aware commodity entries with credits in the
heading, numeric cargo usage, unit buy/sell prices, stock, station buying demand
and held quantities. Commodity letters retain their meanings across pages. Illegal
cargo and regional price-event labels are shown together, and prohibited purchases
at ordinary stations are labelled unavailable in both catalog and details. A manual
unadvertised buy key still rejects without changing state. Completed/rejected trades
return a retained result to the catalog; cancelled quantities preserve its prior
result. Existing checkpoint-before-acknowledgement and trade rules remain unchanged.

Futures catalogs use complete-choice pagination with up to four selections per
page, read-through continuation for oversized labels, and nonselectable fee/limit
notices. Returning from inspection preserves the list page; a completed action
returns to its retained result. Signing drafts and existing-order details paginate
with credits and Back available, preserving quantities/terms through paging and
rejected input. Draft edits write nothing; signing and cancellation retain their
existing final confirmations, fees, pickup behavior and legacy-order terms. Their
durable results remain visible in the futures catalog and on return to the market.

Faction contacts remain accessible at every station through [P] Concord and
[W] Blackwake, before and after membership. These are read-only, paginated screens
until a deliberate Join action and final confirmation. Each shows current standing,
joining requirements, the once-only 2,000-credit grant and exact perk conditions.
Joining validates eligibility before mutation and checkpoints before acknowledgement.

Dual membership is explicitly permitted, including existing careers: Concord and
Blackwake retain independent membership and standing. Neither membership is erased
by hostile conduct, but a faction suspends its own perk at standing -50 or below;
it resumes automatically above -50, without another joining payment. Concord's
active commission adds 25% to bounty/escort payouts, evaluated when they pay.
Blackwake's active membership halves the chance of a new customs inspection.
Already resolved encounter decisions remain cached on reconnect. Membership does
not excuse notoriety, clear fines or override the other faction's standing.

These conditions apply to existing memberships as well as new ones. The pilot
record and contacts show active versus suspended perks, and bounty/escort details
explain that their quoted bonus follows standing at payout. Historical highlights
are retained as records of what was said when earned. Retirement clears membership
as before. Authored faction assignments build on these contacts in a later slice.

Each faction contact offers one optional authored case per career through [S]
Story. Cases require no membership, entry fee, ordinary contract slot or deadline.
After acceptance the pilot visits an existing workshop for evidence, chooses an
irreversible course of action, then visits the stated destination to complete it.
All choices show destination, material requirements, gross payout and current
capped standing effects before commitment. Cargo handovers receive a final
confirmation and use ordinary delivery FIFO accounting.

Concord's Missing Dispatch begins at Rivet House. Filing for prosecution returns
the evidence to Freeport for 1,800 credits, Concord +18 and Blackwake -12. Patient
relief instead delivers two Medicine to Far Lantern for 1,500 credits, Concord +12
and Blackwake +6. Blackwake's Broken Toll begins at Tuning Fork. Armed enforcement
delivers one Weapons to an existing Haven for 2,100 credits, Blackwake +18 and
Concord -12. A public beacon instead delivers three Electronics to Far Lantern
for 1,500 credits, Blackwake +12 and Concord +6. Standing remains within -100..100.
These fixed case rewards do not receive commission bonuses. Each ending records
one completed mission, a named career highlight and a distinct closing response.

A separate faction-stories-v1 namespace selects the Haven without changing galaxy
or encounter randomness; if no Haven exists, armed enforcement is unavailable and
the beacon alternative remains available. Only an accepted case's current bearing
permits travel to an otherwise uncharted case destination. It reveals no market
quotes. Two optional versioned case records retain accepted/evidence/committed/
complete stages and the chosen ending; incompatible or invalid state is preserved
for recovery. Acceptance, evidence, choice and completion checkpoint before their
results. Browsing, refusal and Back write nothing; retirement clears both cases.

Crew are recurring named specialists, with a concise personality and persistent
service record per role. The `crew-v1` identity namespace chooses from fixed
role-specific candidates without galaxy or encounter RNG. Browsing previews a
candidate without creating a record; hiring records that identity. Existing hired
crew without records retain their base bonuses and begin recorded service at the
next paid jump. Earlier service is unknown, never guessed. Wages and hire prices
remain unchanged.

Five, fifteen and thirty paid jumps promote a specialist from Recruit through
Seasoned, Veteran and Ace. Recorded service caps at thirty. A gunner adds one
extra damage per promotion above the base three; an engineer saves 25%, 30%, 35%
or 40% of base jump fuel (savings rounded up, minimum one fuel burned); a navigator
adds one to four survey hops. Unhired crew provide no bonus. Promotions commit
with the departure that pays wages and are acknowledged afterward; engine fuel
is already spent for that departure, so its improved efficiency starts on the
following jump. Route budgets use current efficiency and can conservatively
overestimate later fuel when a promotion occurs en route.

Dismissal or unpaid resignation preserves the named record and service; rehiring
costs the ordinary hire fee and restores the same specialist. Hiring, dismissal
and salary progression validate or resolve synchronously, with UI/travel owners
checkpointing before acknowledgement. Only three optional versioned records are
stored on the ship, with bounded candidate identity and service count. Malformed
or unsupported records enter preserving recovery. Existing destruction/stranding recovery keeps hired crew and their records. A
fresh or retired career has no crew records. Existing interrupted travel never repays wages or grants service
again; cached combat with absent records retains its prior crew bonus.

Each Seasoned-or-better specialist offers one optional personal assignment per
career, reached from the crew roster's numbered task entries. Acceptance is free,
uses no ordinary contract slot and has no deadline. Completing it requires that
specialist to be hired and the pilot to visit the named existing workshop. The
gunner brings one new combat recording to Rivet House for 600 credits; the
engineer delivers three Machinery to Tuning Fork for 900 credits; the navigator
brings up to three newly charted systems to Far Lantern for 700 credits. If fewer
than three systems remain, all remaining systems suffice; a complete atlas can
be delivered directly. Combat and chart progress use acceptance-time baselines,
so earlier accomplishments are not counted as new work. Progress can be gathered
while a specialist is off the roster, but handover requires rehiring them.

Every assignment awards one completed mission and a personal career highlight,
once. It grants no extra faction standing, crew service or module tier. Material
handover uses ordinary delivery FIFO accounting and warns about shared contract
cargo before its final confirmation. Task state survives dismissal and salvage
recovery, and clears on retirement. Optional versioned task records are bounded
and validated with the crew record; no new galaxy/event randomness is consumed.
The detail page shows the complete objective, progress, reward, crew obligation
and public route before any action. Browsing and Back write nothing. Acceptance
and completion checkpoint before their retained result appears.

Three specialist workshops give existing stations distinct outfitting roles:
Iona Rusk's Rivet House expands cargo bays, Oren Vale's Tuning Fork tunes engines,
and Dr. Sel Parn's Far Lantern builds scanners. A separate `workshops-v1` seed
namespace assigns distinct non-Freeport stations, preferring their matching
economies and a few hops from home. This does not change galaxy generation,
ordinary station services or encounter RNG. Their public directory gives route
bearings without charting those stations or creating market observations.

Workshops install the next ordinary module tier for 65% of its normal credit
price (rounded up) plus materials: two Refined Metals per resulting cargo tier,
two Machinery per resulting engine tier, or one Electronics per resulting
scanner tier. Materials and travel can outweigh the cash saving, so the quote
shows the ordinary price, required and held materials, and reminds pilots to
compare total costs. Inputs may include cargo promised to delivery contracts.
The pilot must visit the workshop; maximum tiers and upgrade effects remain the
ordinary module rules. No extra fuel, repairs, reputation or mission completion
is granted. Installation is a deliberate, confirmed action, bounded by module
caps; cancellation, directory browsing and route previews write nothing.

Validation precedes credit/material consumption. Existing FIFO cargo accounting
records workshop materials separately from losses, and records workshop credit
spend separately from trading profit. Legacy ledgers default these additive
counters to zero. The module tier, costs and named workshop career highlight
checkpoint together before success is shown. Career retirement clears these
ordinary module and ledger values. The work order itself adds no day or RNG draw;
travelling to a workshop follows the usual risks, fuel and wage rules.

Voidrunner's optional Archive assignment connects Freeport with the existing
seeded landmark. Mara Venn offers the assignment through Archive Contacts. It has
no deadline, deposit or ordinary contract-slot cost. Accepting reveals a navigable
bearing without charting the destination; recovering the record requires visiting
the site, not merely scanning it. Each of the four existing landmarks has its own
record and interpretation. Previously investigated sites can supply a transcript
without paying their salvage twice.

On returning to Freeport, the pilot chooses public preservation (500 credits,
Concord +5) or a private sale (1,500 credits, Blackwake +5, Concord -2). Standing
is capped within -100 to 100; previews use current standing and results report
the effective change. Each ending awards one completed assignment and a career highlight, once. The contact's closing
response reflects that choice. Optional versioned flags record acceptance, recovery
and mutually exclusive endings; recovery requires an investigated landmark. Every
action validates before mutation and checkpoints before acknowledgement. Browsing,
routing and Back do not accept or finish the assignment. Ordinary travel rules
remain in force and the story consumes no encounter or galaxy-generation RNG.

Landmark inspection itself is screen-first: show its story and unclaimed reward,
then let the pilot Investigate or Back. Existing salvage amounts and seeded sites
remain unchanged; a direct repeated or remote claim is rejected.

Long-range scanning is a deliberate area survey. With a scanner installed, the
chart's Scan action opens read-only terms showing two fuel required, remaining
fuel, range and the number of uncharted contacts. Survey charts every new system
within 2 + scanner tier + navigator bonus connection hops, in stable distance/ID
order. It advances no day and consumes no encounter RNG; no contacts or insufficient
fuel means no charge. Valid survey contracts complete through ordinary discovery
rules in the same checkpoint as fuel spending and chart updates, but a contract a
scan completes pays half its reward (`REMOTE_SURVEY_PAYOUT_PERCENT`); arriving
pays it in full. At full pay a scanner made surveys close to free income -- two
fuel, no jump, no day -- and the contract asks for the trip (issue #648). The
contract details and the scan terms both state the half-pay figure.

The report retains each discovered system's name, station, economy and danger,
plus mission outcomes, using height-aware pages. It creates no remote price
observation. Back before surveying writes nothing and preserves the chart's prior
result. No galaxy generation or existing discovered state changes on load.

Derelict and distress decisions page complete terms with credits, fuel and actions
available on every page. Browsing and invalid input have no effects. Derelict
boarding discloses its 70% salvage / 30% ambush split, credit range and opponent
tier range; new salvage and ambush generation use the destination sector danger.
Cached opponents retain their stats. Distress calls disclose the existing capped
2-4 fuel cost, 60-180 credit reward and up to +3 Concord standing (capped at
100), including an empty-tank warning. Ignoring either encounter continues the journey without a penalty.

New bounty interceptions offer identification in the combat screen before engaging.
Verify costs one fuel and reveals whether the posted identity matches; Withdraw
continues the journey while keeping the contract. A verified mismatch can be closed
without combat, bounty payout, mission credit or notoriety. Firing unverified or
knowingly at a mismatch retains the full bounty but carries the stated notoriety
and Concord-standing consequence. Once engagement begins, identification and free
withdrawal are unavailable. No automatic, undisclosed post-kill identity punishment
is applied to a target already selected under the older rules.

Warrant truth is deterministic per career/contract, using a separate versioned RNG
namespace, and is cached with checked/engaged flags. It never rerolls on inspection,
withdrawal or reconnect, and it consumes no encounter RNG. The historical bounty
completion RNG slot is still consumed after a win for sequence compatibility.
Verification fuel, flags and retained result checkpoint before acknowledgement;
closing a mismatch checkpoints its terminal result before the parent removes the
contract. Validate warrant versions and reported-outcome consistency on load.

New two-raider encounters use a versioned coordinated formation. Before the first
combat decision, Target switches which raider to engage first without spending a
turn. Both opponents and the covering-fire contribution are disclosed. While both
remain, the other raider adds 2 + twice its tier to a surviving target's return
fire, after shields and before Guard reduction. Killing the target prevents that
exchange's return fire and ends covering fire for the remaining duel. Guard
therefore protects against the pair, while target selection can remove a fragile
or dangerous partner first. No healing occurs between foes; successful evasion or
bribery breaks contact with the whole squadron, as before.

Formation metadata is attached when a new pair is generated, and every saved
squadron carries it. Target selection checkpoints
order and combat state before acknowledgement and closes after any valid combat
decision. Formation creation and target changes consume no encounter RNG.

New Voidrunner fights use a versioned tactical ruleset with three deterministic
opponent patterns: Raider (attack, volley, recover), Bulwark (cover, volley,
recover), and Skirmisher (harry, attack, volley). The current intent and incoming
damage range are visible. Fire deals full damage and recharges Guard; Guard fires
a reduced shot and cuts incoming damage to a quarter, then requires Fire before
reuse. Cover/harry reduce outgoing damage, recovery exposes the enemy, and harry
reduces escape chance. Failed evasion/bribery receives and advances the same intent.
New damage curves smooth starter tier-2 fights and make high-tier fire consequential
for heavy hulls; enemy HP is not inflated to lengthen fights. Ruleset version 2
(issue #406) changes the per-tier threat bonus from 0/3/6/20/55 to 0/3/6/8/34:
version 1 dropped a bracing starter Shuttle from a 99% win at tier 2 to none at
tier 3 between adjacent danger ratings; under version 2 that Shuttle wins about
half its tier-3 fights with Guard (and almost none without), a Cutter guards
through tier 4 about half the time, and a maxed Carrier still loses about 40%
of its hull to a tier-4 squadron, so heavy hulls keep paying for fights.

Ruleset version 3 (issue #647) keeps version 2's damage and adds a way out of a
fight the ship cannot win. A raider *outclasses* a ship when two unguarded
volleys would break its full hull; for a Shuttle on its factory hull that is tier
4 and nothing below it, and hull reinforcement is a real answer to it. An
outclassing raider is after the cargo, not the wreck: Dump gives up half the hold,
chosen at random, and breaks contact three times in four, and Evade has a floor
of 30%. Harry still takes ten points off either. The combat panel marks the
contact OUTCLASSED and prices the Dump it will actually make, and the chart's
departure prompt says "Raiders there can outclass this hull" when the
destination's worst raider (its danger plus one) would. Found in play: the first
profitable market of a career sat in a danger-4 system one jump from the First
Flight destination, and under version 2 a starter Shuttle there had Evade at
5-14% and Dump at 24-34% against an attack worth two thirds of its hull and a
volley worth all of it, so the only available lesson was never to go. The rules
are for raiders: a Concord patrol is not after the cargo, takes no Dump and has
Surrender as its way out, so its odds are unchanged and its panel is not marked. In trials a Shuttle with a full hold that
dumps and runs now survives a tier-4 contact 94 times in 100, giving up thirteen
units on average, against 48 under version 2; with an empty hold it is still a
coin toss, which is what the departure warning is for. Rejected: lowering tier-4
damage (it is what keeps a Carrier honest), and moving dangerous systems away
from the opening neighbourhood, which needs a new galaxy version and would make
the risk rarer without making it any more survivable.

A fight keeps the ruleset it started with; every listed version loads, others
are unsupported. Automated trials establish the curve; human playtesting still decides its feel. Existing rewards,
ship upgrades, faction consequences and squadron sequencing remain in force. Newly
generated raiders use destination danger for both squadron chance and tier; stored
opponents retain their stats, and tier/name RNG draw ordering stays unchanged.

The tactical pattern step and Guard readiness checkpoint with opponent HP and the
last exchange. Pattern selection consumes no extra RNG. Every fight uses the tactical
rules and every saved fight carries its tactical state. Reject malformed or
unknown tactical versions through preserving recovery. Automated probes establish bounded fight lengths and
mechanical tradeoffs; human playtesting remains required for engagement/balance.

Voidrunner customs inspections show complete surrender/bribe terms in height-aware
pages: detected contraband, current credits, the conditional bribe payment and
60% acceptance chance, confiscation/fine on refusal, and standing/notoriety effects.
A fine is capped at credits on hand and creates no debt. Unaffordable bribery is
unavailable; typing its key is harmless and retains the inspection without RNG,
cargo, credit or reputation changes. Valid decisions keep the existing outcomes
and checkpoint their effects and completion before acknowledging them. Browsing
and invalid input preserve the pending inspection across disconnect/restart.

Voidrunner combat uses height-aware pages with numeric opponent/hull, fuel and
cargo telemetry and current credits in the heading. The last checkpointed exchange
remains available while paging or toggling tactical details. Browsing consumes no
combat turn or encounter randomness. Escape odds, conditional bribe payment and
refusal retaliation, random one-unit cargo sacrifice and patrol surrender terms
are visible before choosing an action. Existing action hotkeys work across pages;
browsing preserves encounter RNG and resumable state. The combat action bar
labels every verb like the other detail screens (`[F] Fire [G] Guard [E] Evade
[D] Dump [P] Pay bribe ... [I] Info`) instead of a bare letter list, and Dump appears
only with cargo aboard; with an empty hold `D` is not a displayed action and
does nothing (issue #414). Every verb is named at every supported size: the
compact letter list this kept below 40 columns went with those terminals
(issue #495).

Every path that ends an active contract without payment records it (issue #403):
failed escort or bounty fights and abandonment count as failed, deadline expiry
as expired, each once and in the same checkpoint that removes the contract, with
a log line naming the forfeited reward; contracts still active at retirement
count as abandoned in the dossier and are named in the new career's log, the
finale screen says how many, and the retiring save is not mutated so a
cancelled retirement leaves nothing behind. Closing a verified-mismatch warrant is neither. The pilot record shows completed, failed and expired; dossiers written
from this version carry both counts (older dossiers carry neither and still
load). During an escort fight the Evade, Dump and Bribe lines say the contract
fails.

Destruction charges a salvage fee of 200 credits plus four per point of maximum
hull, capped at credits on hand so it never creates debt; it loses all cargo and
tows the ship to Freeport (issue #402). A fee paid in full restores full hull; a
pilot who cannot pay it all gets a quarter of maximum hull plus the paid fraction
of the rest, which stays below full hull until the whole fee is paid, so an
empty account buys a flyable ship rather than a repair. That floor never rises
above the hull the ship carried into the fight: the tug patches, it never
upgrades, so a wreck cannot hand a broke pilot hull the yard would have charged
for. The fee is always dearer than repairing before the fight, so losing is
never the cheapest repair at any balance.
A Concord patrol collects the outstanding notoriety fine on top of the salvage
fee and closes the file only when both are paid; an unpaid fine leaves the
wanted status standing, and a raider kill never touches notoriety. Surrender
therefore remains the cheaper way out of a wanted status at every notoriety
level, because it costs the fine alone and keeps the cargo. The combat page
names the fee beside its low-hull warning, the Info view states both rules and
the fine a patrol will collect, and the deck's critical-hull line names the fee.

The title splash and the registration box follow the portraits' rule (issue
#404): the large composition with the block logo appears only when it fits the
negotiated width; otherwise a complete compact composition (wordmark, wrapped
subtitle, stacked node/pilot/galaxy fields) is shown, and every box row has one
display width, including for callsigns with wide characters. The registration
greeting is drawn in one color for the whole of its text, with the callsign the
only part picked out: a sentence that wraps must not change color between its
rows, and body text inside a box is never left in the terminal's default
foreground, which on a caller's client can be the color the box border itself
is drawn in.

Every paged screen draws inside the tactical HUD's box (issue #486). Making the
screens responsive to a negotiated terminal had spent the frame, the cockpit's
status band and its gauges, which no issue had asked for; the box is back in the
one place all twenty-four paged screens draw through. Its cost is part of the
page-capacity measurement -- one row for the bottom border, and four columns for
the sides, the indent and a right-hand gutter -- so a framed page still fits the
height it was negotiated for, and a keyed entry is wrapped narrower again to
leave room for its `[K] ` prefix. The title is drawn into the top border with the
page counter. A title is never cut: where the border cannot hold the header, the
page draws a plain border and puts the whole header in its first row instead,
and that row is charged to the capacity like any other. A screen whose title
will not fit a 40-column border names itself more briefly rather than spend a
body row on a header -- the Hall of Fame's `Completed careers` becomes `Fame:
Completed careers` -- measured against the widest counter it can reach, so the
title cannot change under the caller. The deck's hull/fuel/hold gauges and the
fight screen's opponent and hull gauges are back with the frame, and give way to
plain numbers -- never the other way round -- on a row too narrow for both.


**Achievements (accolades in the code).** Sixty-six goals in twelve groups:
ladders (10/100/1,000 jumps; first, 10, 50 and 100 victories and contracts;
market margin; peak credits; charted systems; contraband milestones), one per
rank above the first and per career ending, faction commissions and stories,
ship milestones, encounter deeds, combat feats, and a hidden Misfortune group
of wry ones. They are distinct from the Hall of Fame's achievement views,
which rank careers against each other; these are goals a pilot completes.
They belong to the account, not the career: kept through retirement with
their own counters, and measured across every career at once -- running
totals add each retired dossier to the current career, bests take the best of
any, and "in one career" goals take the best single career. An achievement
unlocks in `World.commit`, written by the save that earned it and announced
only once that save succeeded, so it can never be shown and then lost. A career
that already did a thing unlocks it at its first commit on a build that knows
it; what only a new counter can know counts from that build on. The deck names
up to three unlocks and plays one medal scene, struck in bronze, silver or gold
for the best of them; more than three, as a returning veteran catches up, are a
single count. A rank's achievement is the promotion's own moment and does not
play a second scene. Pilot Record `[A] Achievements` lists them all by group:
earned ones with the career that earned them and how many of this board's
pilots hold them, the rest with their progress. Progress is drawn as a gradient:
the goal in its medal's color (bronze, silver or gold) and the count so far a
step closer to that color from plasma for every tenth of the way. Hidden ones
are only a count until earned. The full action bar needs 96 columns, so a narrower terminal gets
`[<] [>]` for paging and `[A] Awards`, which keeps the bar on one row at 80. Rarity reads the score records the Hall of Fame already keeps;
a record written before achievements existed is left out of it rather than
counted as a pilot who earned nothing.


## Terminal floor and presentation review

**Voidrunner requires a terminal of at least 40 columns by 12 rows, and has
exactly one layout above it (Thiesi/NetBBS#495).** A launch on anything smaller
is refused, by name and with the size the terminal reported, before any save is
opened. The door used to carry a second, stripped presentation for terminals
down to 20 columns, and that is what flattened it: art, gauges and rules were
dropped so content would fit a caller nobody dials in from, and the stripped
result was then what an 80-column caller saw as well. A narrow case may never
set the ceiling again -- which is why there is no second layout to leak upward,
and why a screen that will not fit 40 columns is redesigned rather than given a
compact variant.

Door presentation is reviewed by looking at rendered screens.
`tools/door_gallery.py` drives every screen in a subprocess at 80, 64 and 40
columns in each display preset and writes an HTML page through
`tools/ansi_to_html.py`; a change to a screen comes with that page. The test
suite can only assert that a screen fits -- never that it looks like anything --
which is how an entire visual design was lost with every slice passing review.

## The Voidrunner presentation contract (issue #493)

Voidrunner is the showcase for what a NetBBS door can look like, and after the
#310 overhaul it was a sequence of grey text walls behind hotkeys. The
presentation half of that overhaul is this contract. It is normative for the
door; Retro Trivia is the floor it must clear, not the target.

**Color reaches the body.** Every body row of every paged screen carries
styling. The single mechanism that deleted the game's color was the shared
paginator: `wrapped_group` wrapped each row through an ANSI-stripping helper
before it was printed, so the frame was the only styled thing a page could
have. It now wraps styled text, carrying the active color across a break, and
`draw_page` colors by role anything that still reaches it plain -- a screen
that builds its own rows cannot opt out. This is asserted, at 80 and at 40
columns, on the deck, market, yard, record, board, chart, crew, display,
customs and combat screens.

**Ten roles, not ten colors.** What a token *is* decides its color. `hull`
`#7fa3bf` frames, section headers and station names; `deep` `#2f3844` frame
shadow, gauge tracks and separators; `plasma` `#ff5abe` the brand, the rank and
the cursor; `gold` `#ffc83c` hotkeys and credits and nothing else; `ink`
`#e8f0ff` values -- the thing the caller reads off the row; `slate` `#b39b7d`
labels and units; `text` `#bcc4d0` running prose; and `mint` `#6cf2a0` / `amber`
`#ffb347` / `alarm` `#ff5c6c` for good, caution and danger on gauges and
severity glyphs. A hotkey is always gold and bold; a value is always ink; a
label is always slate; a sentence is text; chrome is never the color of content;
one accent carries the eye per screen. That the hotkey, label, value and frame
colors differ, and that all four appear on a drawn screen, is asserted rather
than eyeballed.

The label role is deliberately the one drawn from outside the cockpit's own hue
(issue #519). It was `#7f8fae` -- a desaturated *blue* on a blue screen -- so a
label read as the same color one shade down rather than as a different kind of
thing: measured on the command deck, `deep`, `hull`, `ink` and that label color
were 78.7% of visible characters in one band. Warm rather than neutral because a
label has to separate, not merely differ, and desaturated so it cannot be
mistaken for `gold` or `amber`, which mean money and hotkeys. War Dialer's own
label role made the same mistake in green and was moved the same way.

Chrome recedes (issue #519, the second half). Moving the label role changed
8-11% of the screen; the remaining mass was the frame. `hull` was `#5fd7ff`, a
saturated cyan, and `deep` was `#1d3b57`, a saturated dark blue; together they
were 49.7% of the command deck's visible characters, 85% and 99% of them
box-drawing. Chrome that bright competes with the values drawn on it. `hull`
is now steel -- the same hue at half the saturation and a step down in
brightness -- and `deep` is a near-neutral dark, so a gauge's filled half is
the only colored thing on its row. Both remain distinct from every content
role; the sixteen-color preset reads them as dark cyan and bright black. The
before/after is judged from `scripts/door_gallery.py`, not from the hex; the
command deck and the market, before above and after below, are kept as
`docs/images/door-chrome-519-voidrunner-deck.png` and
`door-chrome-519-voidrunner-market.png`.

Prose is not a label (v1.2.2 play-test). Everything `style_body_line` could not
place used to fall to `slate`, so a screen made of sentences -- the Pilot Guide,
the viewport, a contract's terms -- was nearly all the label color, and the
labels it exists to mark disappeared into it. Prose is now `text`, a cool light
grey a step below `ink`. A row shaped `Label: value` is read as one: a label of
up to four words, opening the row or a clause, is slate, and the clause it
introduces is ink up to the next full stop or semicolon, because what follows
is usually a hint (`Deadline: none. Jumps advance the day.`). Five words before
a colon is a sentence (`Estimate is not total profit: ...`). Tables and composed
rows were never affected; they choose their own roles.

A preview count inside a label is a *value*, not part of the label: the service
menu writes `Board: 4 offers` with the count in `ink` between two `slate`
fragments, because the count is what a caller opens the menu to read. Without
the colon the label read as a sentence -- subject "Board 4", verb "offers" --
and a caller asked what the numbers meant (issue #518). Counts agree in number
with their noun.

**A value is a value whether or not it is a number** (issue #532). The rule
above is the shape of a recurring mistake, and the count in a menu label was
only its first instance. `style_body_line` assigns color by token *shape* --
digits, credit figures, gauges, severity words, `[K]` -- and anything it does
not recognise falls to the prose role. So a value that happens to be a word had
nothing to match and came out the color of its own label: `HULL ... 60/60
Intact`, `Concord Navy ... +0 Neutral Not joined`, `Crew none`. Two style lists
made it worse by declaring those columns `label` outright. A column that holds
what the caller reads off the row is a value column, whatever its cells look
like; absent severity means *value*, not *label*, and only the bar carrying that
same absence stays chrome.

**A table's headings are structure, not labels.** They take the `heading` tone,
which is `hull` -- the role that already means section headers -- so a table
announces itself as one instead of being the color of the sentence above it and
the footnote below it.

**The action bar wears the roles of the menu it mirrors.** A footer is a row of
`[K] Label` pairs, not a sentence with keys in it, so `out_prompt` styles it the
way `key_label` styles the service menu: gold key, value label. It is done in
`out_prompt` rather than at the thirty call sites that print a prompt, and a
prompt that styles itself is left alone -- the same rule a composed body row
follows. Before this, the action bar was the only row on a Voidrunner screen
carrying no palette role at all, and it is the row a caller has to read to know
what to press.

**A screen of figures is not a screen of sentences.** The Trading Ledger was
fifteen prose rows with `HOLD -`, `TRAVEL -` and `LOCAL MARKET -` as headings
wearing a hyphen; 73% of its characters were the prose role and 0.4% were
values. `section` and `table` exist for exactly this, and every fact it carried
survived the change of shape. Where a table's rows each hold a single figure,
no column may be `optional`: dropping one leaves a row with nothing on it, which
is what a first cut of that table did to `Cargo lost or surrendered` at forty
columns. With nothing droppable the table stacks instead, and a stacked row
still carries its figure.

**A menu is built, not pattern-matched.** The Hall of Fame wrote its view
switcher as the sentence `Views: [1] Wealth, ... [5] Completed careers.`, and
`Completed` matched the good-tone severity pattern, so one view name rendered
green for no reason. A component's decision beats a pattern's guess -- the rule
that settled the preview counts -- so the switcher is a `menu_grid` under a
`section` rule.

**A screen replaces the one before it** (issue #516). A door owns the terminal
for its whole session, so every paged screen clears and repaints rather than
printing underneath its predecessor: a session was one long scroll, and a
caller's terminal history filled with superseded copies of the command deck.
The clear lives in `draw_page`, the single path every paged screen already goes
through, not sprinkled per screen -- a per-screen rule applied to most screens
and not the rest is how the color was lost one slice at a time. It is
unconditional rather than following the host's `redraw_in_place` preference,
which is opt-in because clearing a *menu* costs scrollback a caller may still
want; a door's own screens are not that, and War Dialer has always cleared.
The hand-off into a door does follow the preference: the host clears before
launching for a caller who redraws in place, because the door is a new screen
and its title card otherwise arrived under the remains of the door picker
(issue #648). A caller who scrolls keeps the picker in their scrollback.

What that replaces has to survive it. The launch banner is wiped by the first
deck the caller lands on, and everything it said is on that deck already -- the
station, the day and the credits in the status band, the tracked contract in the
alerts -- except a futures order that has come due, which was the one thing on
it a caller had to act on. That is an alert row now, where the contract says
something a caller must answer belongs.

**Truecolor is the design target**, degrading to 256, to 16 (`basic`), to
monochrome, to plain ASCII, in that order, each deliberate rather than
accidental. The only effect that truecolor buys outright is the title splash's
gradient rule, which degrades to a single role color rather than being
approximated.

**One glyph vocabulary, every glyph with an ASCII substitute.** `╭─╮ │ ╰─╯
├─┤` frames; `█░` gauges; `▁▂▃▄▅▆▇█` sparklines; `⟦ ⟧` chips; `◈` credits;
`▲ ◆ ●` severity; `◤` the brand; `●○` crew pips; `→` a delta. A screen asks for
one by role -- `glyph("danger")` -- rather than typing the character, so the
`plain` preset is a designed rendering and a new glyph cannot arrive without
its substitute. No Unicode from the vocabulary may reach a `plain` terminal.

**A component library, local to the door.** Voidrunner ships as a single
self-contained file a SysOp can point straight at, so this is the game's own
vocabulary rather than something shared: `gauge`, `sparkline`, `chip`, `badge`,
`table`, `menu_grid`, `alert`, `status_band`, `portrait`, plus `section` for a
named rule across the page frame. Every screen is built from them, which is what
makes the contract enforceable -- a gauge is the same gauge on the deck, in the
yard and in a fight, and a later slice cannot flatten one screen without
flattening all of them.

**Tables are tables.** A column starts on the same display column on every row
(right-aligned columns end on one), asserted by measuring the rendered rows.
When a table will not fit, it drops the columns the screen has named as
droppable, worst first -- so a narrow table can carry fewer facts than a wide
one, and a screen naming a column droppable is saying that figure is available
elsewhere (the market's depth figures are on the commodity's own trade screen).
When dropping all of them is still not enough it *stacks* -- each record's first
column on a row of its own, the rest aligned and indented beneath, and on a
further row where one is not wide enough -- rather than overflowing and wrapping
into rubble. Stacking never drops anything: every column that survived the
dropping step is on the record somewhere, so the narrow caller reads those facts
in two or three rows where the wide one reads them in one. The two steps are
different promises, and a screen chooses between them by what it marks optional:
mark a column optional only when its figure is a keypress away (the market's
depth is on the commodity's trade screen; the chart's sector and economy are on
the star map's Info; the yard's post-refit hold is on the commissioning
preview), and leave it un-optional when it is not -- the Hall of Fame's rank,
job and run counts are on no other view, so that table stacks instead. A stacked record stays one paginator entry and moves between pages
whole. A table's column headings are repeated at the top of every later page
that carries one of its rows, and only there.

**Layout.** Three menu columns at 72 or more usable columns, two at 52 or more,
one below. Numbers right-aligned. One blank row between logical groups. The
action bar outside the frame, where the cursor waits.

**Motion is in, and it replaces the old "no animation delays" rule.** Reveals,
gauge drains, counter ticks and rank climbs are permitted under three
conditions, none of them negotiable: any keypress ends the effect immediately;
no effect may delay a commit or hold up input; and every effect is absent from
the presets that exist because a caller wants less -- `fast`, `mono` and
`plain`. A reveal belongs to *arriving* at a screen, never to redrawing one: a
key that changed nothing redraws the same page and costs the caller nothing. An
effect with no live terminal on the other end -- a scripted session, a screen
drawn before stdin is open -- is skipped rather than slept through, because an
animation nobody is watching is only a delay. `fast` is a display preset beside
`auto`: the same palette with every effect off.

**The launch splash is motion too.** Once per launch, after the career's
preset is applied and before the title card, the door plays a full-screen
splash of about two and a half seconds: a parallax starfield, a ringed planet
drawn two pixels to a cell in half blocks, a ship crossing on a cooling plasma
trail, and the wordmark resolving in over it. It follows every rule above but two.
Left to run to its end, it holds its last frame under a "press any key to
launch" prompt until the caller presses a key, or input ends, or the terminal
is resized: a title card that vanished the moment it was complete was gone
before anyone had looked at it. And it is the one effect that takes the key
that ends it: whatever follows is a new
screen the caller has not seen yet, so a key pressed to skip a title card must
not act on it. Keys typed before the splash appears are left for the game, and
a launch that has already shown a refusal or a recovery screen skips it. It
draws only the cells that change between frames, never the bottom-right cell,
and composes a complete picture at every size the door accepts (the large face
from 72x20, a 39-column compact one below). At sixteen colors each color is
read by hue family rather than nearest distance, which kept the planet magenta
instead of grey.

**The voyage has scenes too.** The splash's machinery plays at the moments a
voyage turns on: the jump out (the drive spools up, the stars stretch into
streaks, a flash, the tunnel), a contact (raider, squadron, Concord
interdiction, bounty target or convoy ambush, each with its own alert and
caption), a kill, the loss of the ship, a clean escape by Evade or Dump, a
customs sweep, and the approach to a station drawn by its economy in front of
its banded world. A contract finished on arrival is paid in the docking
caption, ahead of the system's name, rather than in a scene of its own that
would lengthen the hop. Quieter moments get scenes of their own: a derelict
salvaged by tractor beam (or waking up as an ambush), a distress call
answered as escape pods cross the gap, a trader's data burst, a landmark
investigated, a stranded pilot towed home, a hull commissioned in drydock under
a welding seam, a retirement that sails for a rising sun under the
finale's name, and the milestones a career is measured by: a promotion's
chevrons (one per rank held, shown once the save that earned it has
succeeded) and the emblem of a faction joined, the Concord's shield or
Blackwake's diamond. The pilot's own hull class flies in every one. Passing a
derelict or a distress call by plays nothing. A scene
is about two seconds and ends on a captioned frame held for under a second,
then play goes on: unlike the splash it never waits for a key. Otherwise the
splash's rules apply, including the stricter two. A scene takes the key that
ends it, because the screen that follows is one the caller has not seen, and
keys typed before it starts are left for the game. Each scene plays after the
commit it shows, so a resumed session never replays a moment that has passed:
a resumed fight does not announce its contact again, and a kept outcome does
not explode twice. A bribe, a surrender and a withdrawal play nothing. They are
a conversation, not a spectacle. Nothing a caller needs is only in a scene.
Every caption repeats what the hop report or the next screen says. Sixteen
colors may read red here, which the splash never does: a raider, an alarm and a
fireball are red. Pixel colors are snapped to a coarse step, and gradients
cool in steps, so a frame sends only what visibly changed. Each scene stays
under 90 KB at 80x24 truecolor.

Voidrunner offers saved display presets from station Display Options: full palette
using the existing terminal color depth, the same palette with motion off
(`fast`), basic 16-color, monochrome artwork, and plain text with ASCII artwork.
Monochrome/plain suppress ANSI styling; plain maps the whole glyph vocabulary to
equal-width ASCII while retaining Unicode pilot text in the career. Encoding is
an independent saved preference: Automatic uses CP437 for classic drop files
and UTF-8 for standalone launches; UTF-8 and CP437 can be selected explicitly.
NetBBS always receives UTF-8 because the host converts output for its caller.
CP437 output preserves encodable characters and uses explicit, equal-width
substitutes for unsupported artwork. Other user text is transliterated for
display without changing the stored name. The encoding preference defaults to
Automatic in older saves, survives retirement, and checkpoints before applying
or acknowledging a change. Each preset previews
itself on the Display Options screen: the sample beside a preset's name is drawn
the way that preset would draw it, so the choice is made by looking rather than
by reading an adjective. A chosen preset checkpoints before acknowledgement;
browsing and reselecting the current preset write nothing. The validated additive
preference defaults to full palette for older careers and survives retirement.
Apply it after loading a valid career and before its normal title/welcome output;
recovery uses the default presentation until a valid career is available. Motion
is on in `auto` and `basic` and off in the other three, under the conditions in
the Voidrunner presentation contract above.

Economy safeguards (issue #310): Blackwake standing from trade follows each new
250-credit high-water milestone in cumulative contraband sales minus purchases
and new futures outlay/refunds. The step is 250 because the non-Haven demand
pool is capped at 48 units, so a wider step made trade alone need about a
hundred round trips to reach the Cartel. Buying and same-station recycling do
not grant standing; splitting transactions cannot reset milestones. Existing
standing is retained and the new ledger starts at zero for old careers (old
cargo has no recorded acquisition cost). Because the saved counter is standing
already granted rather than progress, each career records the step it was
awarded under and loading re-expresses the count in the current step; a career
that earned two points per 500 credits loads as four points per 250, so
changing the step never mints retroactive standing. Combat/faction rewards
remain separate.

The trading ledger records cargo acquisition costs from this version onward.
Market purchases and collected futures carry their actual paid cost, including
brokerage; older cargo has unknown cost and is never assigned a fabricated basis.
Disposals consume older unknown cargo first, then recorded purchase lots in order.
Partial lots retain integer cost remainders so disposing of a complete lot always
accounts for its exact cost. Market sales and delivery contracts show separate
realized margins for costed cargo and receipts whose acquisition cost is unknown.
Mixed delivery payments are allocated by quantity; these are trading margins,
not total career profit. Lost cargo basis, cancelled-order fees, actual fuel
purchases and paid crew wages are reported separately. Unrecorded historical
activity, repairs, fines, hire costs and other career income are excluded.
The ledger resets with a new career, checkpoints with each affected action and
is read-only when viewed.

Docked checkpoints remember visible local buy/sell quotes with their observation
day; browsing never refreshes remote data. Earlier discoveries have no inferred
price history. Trader data-burst encounters also retain the actual quote they
reveal, checkpointed with the encounter result without changing RNG call order.
Each commodity keeps its last observed quote, including contraband
previously carried through a station that does not openly sell it. The ledger's
route estimator compares a new purchase or existing hold cargo with a remembered
destination sale quote. It displays the route, quote age, acquisition basis,
replacement fuel at 6 credits/unit, crew wages, cash shortfall and estimated
margin. Unknown acquisition costs suppress a total-profit claim. Refuelling is
budgeted at intermediate stations where necessary; a leg exceeding tank capacity
is infeasible. Unknown intermediate systems retain unknown names/threats. Prices,
encounters, detours, repairs, other income and contract deliveries can change the
outcome; estimates are advisory and never buy cargo or launch travel.

Route parameters use an explicit draft editor: destination, commodity, quantity
and cargo source are edited together, then applied to the read-only estimate.
Rejected combinations retain the draft; Back discards edits. Applying a route
draft never purchases cargo, launches travel or writes a career. The standalone
door implements this synchronous editor contract locally, because the host's
async resource editor requires Session and DatabaseLane objects unavailable in
the subprocess. Remembered station IDs must belong to the saved chart; inconsistent
observations are rejected at the recovery boundary before names are rendered.


Spot-market depth (issue #310): each pilot's station markets have finite stock
and buying demand. A station carries up to 96 units of each locally produced
commodity and 48 of other goods; it buys up to 96 units of each locally demanded
commodity and 48 of other goods. Each pool replenishes by 6 or 3 units per game
day respectively, capped at its maximum. Only jumps advance that clock; browsing,
restarting and splitting trades do not replenish either pool. Sales add goods to
stock up to its ceiling, but buying does not restore the station's spent buying
demand. Existing price drift, spread and economy-event rules still apply.

Limits are per career, matching the single-player economy. Old careers begin
with full pools; no historical transactions are reconstructed. State is additive,
bounded to the existing galaxy and commodities, and computed without new RNG
calls. Read-only quotes do not create or advance records. Spot purchases and
sales validate limits before changing credits, cargo, accounting or prices, and
persist pool changes with the completed trade. Local quotes show stock, demand
and daily replenishment. Remembered quantities are timestamped observations,
never live remote availability; a route cannot promise sale of a load exceeding
observed demand. First Flight must quote immediately available procurement.

Futures remain separately scheduled wholesale consignments with their existing
fee, term, issuing-station pickup and hold-space requirements. A new order
reserves its quantity from the issuing station's spot stock pool at signing and
applies the ordinary purchase price nudge (issue #401): an order larger than the
available stock is rejected before any change, cancellation returns the
reservation to that station's pool bounded by its ceiling, and settlement does
not consume stock again. Orders signed before this rule carry no reservation and
keep their terms, as do legacy remote-delivery orders. Unloading collected goods
still uses the destination's buying demand. Contract deliveries consume their
contracted cargo independently of spot demand.

New economy events affect a bounded region: an anchor station plus up to two
same-economy stations within two jumps. A deterministic selection from the
existing seed/day/event terms chooses the region without RNG calls or changes to
galaxy generation. The event-creation function retains its existing draw sequence;
later gameplay can differ because the affected market state changes. An absent
selected industry falls back to the current station economy and its first listed
trade commodity, retaining that event's original draw count and duration. Existing saved events without
regional IDs retain their original economy-wide scope until they finish. Price
levels, event frequency and duration keep their existing rules; stock/demand
pools do not refill merely because a price event starts or is viewed.

The ledger's Opportunities screen combines public regional news with up to six
ranked outbound spot-trade candidates derived from the pilot's remembered prices.
News gives named affected stations, coordinates, hops, remaining time and an
explicit unknown-danger label where appropriate; this public bulletin does not
chart stations or create market observations. A boom suggests bringing that
commodity; a crash suggests investigating cheaper procurement. These are leads,
not promises of available stock or profitable resale.

Trade candidates use current local stock/price/hold space, remembered destination
demand when available, fuel and wages, and a cash budget sufficient to reach the
sale. They exclude infeasible routes and loads consumed by active deliveries,
rank positive estimated margins per outbound jump, and open the existing route
estimate/draft without buying or travelling. Quote age, unobserved buying capacity,
legality and excluded return travel remain visible; no live remote prices are
queried. The board is read-only and paginated. Human play remains necessary to
validate whether these opportunities encourage satisfying route variety.

The engineer costs 200 credits to hire and 2 per jump. Fuel savings follow the
paid-service progression defined above; one-unit jumps cannot benefit, and
hiring does not refund old hire costs. Because most adjacent jumps cost one unit,
fuel alone was worth under a credit per jump (issue #409), so the engineer also
cuts yard repairs from 4 credits per hull point to 3 (Recruit and Seasoned) and
2 (Veteran and Ace); the yard and crew screens show the current rate.

New futures orders lock goods for pickup at their issuing station after maturity.
Up to eight orders may be active; insufficient hold space leaves a ready order
waiting. Orders can be cancelled remotely for their recorded goods principal;
the 8% brokerage fee (rounded up per unit, at least one credit) is never refunded.
Splitting orders cannot reduce that per-unit fee. Purchase screens show quantity,
term, pickup station, principal, fee and the station stock remaining after the
order before signing, with Back writing nothing.
Mature goods settle on arrival or station entry before mission completion checks.
Every saved order carries its pickup and principal metadata; a document without
it is refused with the rest of its schema-1 career rather than settled early
under terms its pilot did not agree to.

Voidrunner permits one active session per pilot within a save directory. A
nonblocking OS file lock is held from before loading through the final checkpoint;
a second launch reports that the career is already in use and changes no career
or score. Process exit, including forced termination, releases the lock. Distinct
pilots may play concurrently. Lock files remain in place and must not be deleted
while the service is running. This requires a local filesystem with working OS
locks and atomic replacement; cross-host shared directories are not supported.

The resolved save directory is the installation namespace, and it belongs to
one node: `<db path>.doors/voidrunner/`, beside War Dialer's world, unless the
SysOp sets `VOIDRUNNER_SAVE_DIR`. The node resolves it and passes it to every
launch as an absolute path; the door's own `~/.netbbs/voidrunner_saves` fallback
is for standalone play only. That home-directory path was the node default until
issue #648, and it made every node one OS account ran share careers by user id.
On its first start after the change, a node with no override whose own record
names the legacy directory copies it into its own under the legacy directory's
maintenance gate, staged and renamed into place whole. The record is the evidence
the careers are this node's: a brand-new node under the same account has none, and
never adopts careers another node left there, since user ids do not transfer. A
target that already holds careers is never overwritten. It copies rather than moves, because a second node
under the same account may still be reading it; each node then owns its copy. A
busy or unreadable legacy directory is left alone and the node keeps using it
until a later start copies it. Changing a node's display name does not change
career identity. See the door guide for the upgrade and override steps.

Hall of Fame records are retained independently at `scores/<user_id>.json`; only
the displayed ranking is limited to 20. A `leaderboard.json` from before those
records is imported into `scores/` once per save directory, under the maintenance
gate, and the old file is retained but no longer read at runtime. The import
belongs to the launch rather than to a career: a node whose only returning caller
holds a refused schema-1 save still imports its rankings, which is exactly when
they matter most. Scores outlive careers, so they survive the schema-2 cutoff
even though careers do not. Updates replace only that pilot's file using a flushed
private temporary file. The career save also retains the credit high-water mark
across retirement and temporary score-write failures, allowing a later checkpoint
to repair its score. The career save is authoritative for live gameplay state and
per-career achievements. The compatibility import of validated lifetime wealth
and retirement maxima described above is the sole exception: these retained totals
can raise the save's historical floors, and the retirement count affects a later
New Game+ starting bonus. Other score fields never replace current gameplay or
invent missing dossiers. Score writes remain optional projections; failure does
not erase totals already retained in the career save. Historical records already
discarded by older top-20 storage cannot be reconstructed. Node backup coverage and explicit external-directory restore
are specified in section 13.4 and the door guide.

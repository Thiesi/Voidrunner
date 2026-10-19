# Voidrunner engineering notes

Implementation constraints and lessons for `voidrunner.py`: why the code is
shaped the way it is, and what breaks if it changes. They come from NetBBS's
engineering record (`docs/NetBBS-worklog.md` at Thiesi/NetBBS@d0cc3a40), moved
here with the game (Thiesi/NetBBS#1216). The game's rules are in
[design.md](design.md).

Issue numbers written "issue #N" refer to Thiesi/NetBBS. A "NetBBS" or "this
file's own entry above" in the text means that repository's worklog.

`netbbs.doors.bundled.voidrunner` (a second, larger real door alongside
Retro Trivia; both ship as real installed package data under
`src/netbbs/doors/bundled/`, not loose `examples/` files -- see that
package's own docstring) persists a per-caller save file itself, since
the sandbox
gives a door no database access and deletes its scratch working
directory after every session (see this file's own entry above). Two
invariants that follow, both load-bearing for anyone touching that file:
(1) its save only stores the galaxy's random seed, not the galaxy
itself, and reconstructs the ~48-system map by replaying
`generate_galaxy(seed)` on every load -- which only stays correct if the
*exact sequence* of `random.Random` calls inside that function never
changes; reordering, adding, or removing a call anywhere before the end
of that function would silently regenerate a different galaxy for every
existing save (system ids drifting to different names/economies/
connections underneath a `discovered`-ids list that no longer matches).
New randomness there is safe to add only at the very end of the
function. (2) completed station actions must call `World.commit()` after
all their rule changes and before success output or another input read; the
turn boundaries -- launch, docking, the rescue tow and the fresh career after
retirement -- call `World.checkpoint()`, which is turn processing plus that
same commit (issue #417).
`main` binds the storage callback; domain-only worlds can remain filesystem-free,
and career reset preserves the callback. The outer menu loop alone cannot provide
this guarantee because markets, yards and charts own nested input loops. Scans
commit their discovery and mission rewards together; auto-routing checkpoints
each completed hop. Save errors escape to a stop-and-acknowledge screen and must
not be swallowed as successful actions. A private temporary file is flushed
before atomic replacement; this prevents shared-temp collisions but does not
by itself serialize concurrent pilot sessions or leaderboard updates. The
session lease described below provides that read/play/write boundary. Tests must kill the
real subprocess after its success output while a nested menu still owns input;
EOF and orderly menu-exit tests do not prove this boundary.

A Voidrunner journey saves monotonic primary/escort/arrival/customs phases. Every
combat decision commits opponent HP, the event RNG state, ship/pilot changes, and
a terminal outcome together before narration. The parent consumes that outcome
with its mission reward or next-wave index in a separate atomic checkpoint; never
clear the outcome before the parent effects are committed. Escort snapshots carry
the full contract because legacy offers can share IDs. The per-hop destruction
signal persists too: a resumed loss must resolve its failed bounty/escort, skip
later encounters, and retain the tow to Freeport. Only the current hop resumes;
an interrupted auto-route does not continue to later hops without pilot input.
The latest combat exchange is bounded to one decision, not an expanding replay
log. Validate this boundary by serializing every checkpoint of representative
journeys and comparing each resumed result with uninterrupted execution, alongside
a real subprocess kill/relaunch test. The event RNG is independent of galaxy
generation. New journey state has its own version; unreadable/unsupported state
must stop play without entering the older corrupt-save/new-career fallback.
Checkpoints also validate cross-field consistency before any startup write:
remaining bounty/escort snapshots must match active contracts as a multiset,
terminal outcomes must agree with opponent HP and destruction state, and the
saved location must match the journey phase. Completed contract snapshots no
longer need active membership; requiring it would reject legitimate restarts.
The storage boundary also validates career structure and gameplay ranges before
loading or writing, as described below.

`confirm` reads `read_key` directly so it can see the multi-character
`ESCAPE_KEY` sentinel that `read_command` would fold into `IGNORED_KEY`; keep
that when touching either prompt (issue #413).

Voidrunner's menu commands case-fold ASCII only; Unicode remains text input,
so Unicode case aliases cannot surrender cargo or dispatch another hotkey.
Acknowledgement pauses ignore both incomplete Escape and unsupported keys.
Because Escape cancels quantity fields and confirmations, the decoder reserves a
second `_INPUT_TIMEOUT` for a delayed CSI/SS3 introducer before reporting
`ESCAPE_KEY`; a lone Escape costs two timeouts, and a fragmented arrow key over
a slow transport no longer cancels what the caller was typing. Any new consumer
of `ESCAPE_KEY` that acts destructively depends on this window.
After standalone Escape times out, only CSI/SS3 introducers are reserved for
delayed arrow suffixes; P/X are independent hotkeys. Control strings must begin
before that timeout, then retain their payload across subsequent delays.
OSC accepts BEL or ST; other control strings require ST, and bracketed paste
recognizes both CSI encodings of its terminator.

Removing an active Voidrunner contract without paying it must go through
`record_mission_loss` (failed / expired / abandoned) in the same checkpoint as
the removal; a resumed hop must not record it twice, which the escort index and
cached bounty outcome already guarantee. Dossiers carry `failed` and `expired`
as a pair or not at all; the validator rejects one without the other.

Tactical damage reads its threat curve from the fight's own `tactics["version"]`
through `tactical_threat_bonus`; changing a curve means a new version in
`TACTICAL_THREAT_BONUS_BY_VERSION` and a `TACTICAL_RULESET_VERSION` bump, never
editing an existing tuple, so cached fights resume with the damage they showed.

`salvage_fee` must exceed a full repair from zero for every hull class and tier
(`hull_hp_max * 4` plus a base); `destroy_ship` takes `patrol=` from the combat
session and only that path clears notoriety. Any new destruction path must pass
the flag explicitly rather than clearing notoriety itself.
Two bounds keep destruction from beating the alternatives at a low balance.
`destroy_ship` also takes `hull_before=`, the hull the fight started with (the
combat checkpoint carries it), and `salvage_hull` caps its free floor at that
value; without it a wreck is a free repair for any ship whose hull sits below a
quarter of maximum. On a patrol kill the charge is the salvage fee plus
`notoriety_fine_cost`, and notoriety clears only when credits cover both; the
fee alone is bounded by hull while the fine grows without limit, so a bare
patrol amnesty is cheaper than surrendering as soon as notoriety climbs.

A fight keeps the tactical ruleset it began under. `new_tactics` takes the
version as a parameter and defaults to `TACTICAL_RULESET_VERSION`; every
reconstruction of an existing fight's opening state -- the pending-travel
consistency check and squadron target switching -- must pass the saved version.
Comparing a saved v1 checkpoint against a freshly built v2 one makes the career
unresumable after the ruleset changes.

`contraband_trade_milestones` counts standing already granted, not progress, so
it is only meaningful together with the step it was measured against. The save
carries `contraband_standing_step` and `_load_contraband_milestones` rescales
the count on load. Any future change to `CONTRABAND_STANDING_STEP` is therefore
safe; dropping the recorded step is not, because a smaller step would re-award
gains that were already paid for.

`single_page_footer` matches paging tokens by shape (`_PAGING_PATTERN`), not by a
table of literal strings: footers spell them `[<] Prev [>] Next`, `[<>] Page` and
`[N] Next [P] Prev`, and end with a space, the prompt's colon or `" > "`, so a
literal table silently missed most callers. A screen is also paginated twice
when it does not fit: once with the full footer and once with the shortened one,
because dropping the tokens can buy the row that makes it a single page. Any new
action bar that offers paging must be built so that removing the tokens leaves a
valid prompt.

`World.checkpoint()` is turn processing plus a commit, and the two halves are
callable separately (issue #417). `advance_station_state()` expires contracts,
repairs duplicate mission ids and prepares the station's offer board; it writes
nothing and runs only where a turn begins -- launch, docking and the fresh
career after retirement. `commit()` persists, and also captures a rank reached
by the action (before later spending can lower it) and re-observes the local
market (a docked caller who just traded has just watched its stock move). Every
other call site commits only. A new caller that changes the day or the station
must call `checkpoint()`; one that finishes an ordinary action must call
`commit()`, or the board rerolls under the caller.

Voidrunner's color lived and died in one function (issue #493). `wrapped_group`
wrapped every body row of every paged screen through `_mission_plain`, which is
`ANSI.sub("")`, *before* it was printed; the frame restored in #486 was the only
styled thing a page could have, because it is added afterwards. Two rules keep
that from recurring, and both are asserted. First, the wrap is styled:
`wrap_styled` carries the active SGR across a break -- `_wrap_output` is
ANSI-aware about width but leaves a broken span's continuation in the terminal's
default foreground -- and `style_body_line` colors by role anything that
arrives plain. Second, `draw_page` applies that same styling to rows a screen
built itself and handed straight to it, because a rule that held for most pages
and not the rest is how the design was lost one slice at a time. A component's
own styling always wins: `style_body_line` returns a row that already carries an
escape untouched.

Three zero-width control marks say what a Voidrunner row *is*, and the paginator
acts on them: `SECTION_MARK` a named rule drawn across the page frame,
`STICKY_MARK` a table's column headings, `MEMBER_MARK` a row of that table. They
measure zero columns (`_char_width` returns 0 for category C), so a marked row
costs exactly its text, and `draw_page` strips them as it prints -- they must
never reach a terminal, and `plain()` in the test support strips them too.
Headings are repeated at the top of every later page carrying one of their own
rows and nowhere else, which is why the member mark exists: a screen may hold
two tables, and a page of trailing footnotes is not a page of either.

`table_records` is the responsive contract, and it makes two different
promises. It first drops the columns a screen named as droppable,
worst-priority first, and those stay dropped -- naming a column optional is a
screen saying that figure is a keypress away on another screen, and the
alternative is four rows per chart destination at the floor. What survives that
step survives everything after it: if the remainder is still too wide the record
*stacks*, first column alone on a row and the rest indented beneath, on as many
bands as it takes, each band its own little table so a column still starts on
one display column across records. A column that must never go is simply not
named optional -- the Hall of Fame's rank, job and run counts are not. A stacked
record is returned as its own list of rows and joined with `\n` into one
paginator entry, so it moves between pages whole; `wrapped_group` splits on
`\n` before wrapping. The gutter narrows from two spaces to one before a column
is dropped, and a heading that runs to more than one band is not repeated across
pages, because the paginator carries a heading as one row.

Motion (issue #493) asks `_StdioBytes.waiting()`, which *peeks* -- select with a
zero timeout, `PeekNamedPipe`, `kbhit` -- and never reads. Reading a raw byte
would cut a UTF-8 character or an escape sequence in half behind the decoder's
back, and would eat the key that skipped the effect, which is usually the
caller's next command. With no input reader at all, motion reports itself
interrupted: a scripted caller is not watching, and the alternative is a
`time.sleep` per page draw across the whole suite.

Paged Voidrunner screens measure their capacity with `page_capacity` -- the
content column is `_page_content_width()`, the box interior less its indent and
gutter, and the overhead comes from `_page_header_rows` (one row when the header
fits the top border, a border plus the header's own wrapped rows when it does
not), the bottom border, and
the action bar the caller will see -- and fill pages with `paginate`
(issue #418): one implementation that keeps a logical group whole where it fits
and continues it on the next page where it does not. With `keys`, a page breaks
rather than repeat a selection letter, each page carries its own letter map, and
each page's slice of a group is keyed for itself, so a letter always means what
the page printing it says even when an entry spans pages. Page movement goes
through `page_step`, which clamps at both ends and returns `None` for anything
that is not a paging key; it takes the key pair a screen advertises, because `N`
is a live action on screens that do not page with it.

Two screens assign their keys per page rather than per entry -- the mission board
and `_pick_trade_field` both number the choices visible on the page, up to nine --
which `paginate`'s fixed group keys do not model, so they fill their own pages.
They still measure with `page_capacity`'s rule and clamp with `page_step`. A new
paged screen that measures or clamps for itself will disagree with the rest at
some width.

The Voidrunner save validator never builds a galaxy (issue #419). It asks
`galaxy_economies(seed)` and `galaxy_hops(seed, origin)`, both memoised because
both are pure in the seed and neither returns anything mutable. `generate_galaxy`
itself must stay uncached: every caller marks its systems discovered, so a shared
list would alias one career's exploration into another. Every commit validates
twice -- the new document, and the old file before it is aged into `.previous`,
which is what keeps a corrupt save from overwriting the last readable copy -- so
anything a validation does happens twice per acknowledged action.

The pre-`scores/` `leaderboard.json` is not read on the commit path at all any
more, and is not cached: it is imported into `scores/` at launch and the retained
file is re-read on each launch until every row is in (issue #421). What the
retired per-session cache was guarding against still applies to the import
itself, one row at a time: an absent record, a transient `OSError` and a
temporarily unreadable one look identical to `_read_score_json`, so the import
stats the record first and defers rather than replacing what it could not read.

Versioned sub-records go through `_validate_versioned`, which keeps the
distinction the repeated blocks kept getting close to losing: a malformed record
is a `ValueError` (the save is broken) while an unknown version is an
`UnsupportedSave` (the save is fine, this build is too old).

Voidrunner's UI marker is a real boundary, not a comment: nothing above it may
read a key or write to the terminal. Screens therefore live below it -- the four
faction screens were the last that did not (issue #420). A data table heads the
group of functions that reads it rather than sitting between two of them.

`screen_*` is a screen a caller navigates to and can leave; `_screen_*` is a
shared body or a sub-step another screen runs as part of one action (the combat
session behind `screen_combat` and `screen_notoriety_patrol`, the faction contact
behind the Concord and Blackwake screens, the opening offer inside the guide).
Being reachable from the deck is not the test: `screen_travel`, `screen_customs`
and `screen_auto_route` are screens the caller is in.

A validator is named `_validate_*`. `_load_*` reads and returns; the three save
validators named that way returned a coerced value only incidentally and were
read as loaders.

Proving Voidrunner's pilot/maintenance gate needs a second real process: the lock
is a file lease held for the life of a door, so an in-process fixture cannot
observe it. Hold a live door with `_live_voidrunner` and assert both directions --
`maintenance_session` refusing while a pilot is aboard, and a launch during
maintenance reporting busy without creating a career. NetBBS checks the third
direction -- its backup refusing to capture while a pilot is aboard -- in its own
suite, against the vendored file.

`departed` is not a travel phase. It appears in fixtures as an *invalid* phase for
fault injection; the real phases are `primary`, `escorts`, `arrival` and `customs`.

The suite lives in `tests/`, split by subsystem (Thiesi/NetBBS#422). `support.py`
loads the door once, by path, and owns the worlds and the subprocess helpers more
than one slice needs -- the split is by subject, so a helper that crosses subjects
is by definition shared. `conftest.py` owns the fixtures, which is why they are
not imported. A split like this is a move, and the proof is that the set of
collected test ids does not change *once the module path is stripped* -- the path
is part of a node id and necessarily changes, so compare
`pytest --co -q | sed 's/.*:://' | sort` from before and after. Run that check
before the suite, not after: it names what was lost, while a red suite only says
something is wrong.

Voidrunner saves are schema 2 and there is no migration (issue #421). A schema-1
document raises `OutdatedSave`, which is a refusal rather than a recovery case:
`screen_save_recovery` would only offer a `.previous` copy of the same retired
schema. Taking the slot goes through `replace_unsupported_career`, not
`write_save`, because `write_save` decodes what it is replacing and must keep
refusing a career it cannot read; the difference is that this replacement is the
caller's own confirmed decision. It happens after the registration is confirmed,
never before, or a cancelled registration would have destroyed the career, and
the refused document is retained as a recovery copy rather than as `.previous`,
which the launch tick immediately after registration would have overwritten.

The `leaderboard.json` import belongs to the launch and runs before any pilot
lock is taken (issue #421). It needs the maintenance gate, and `pilot_session`
takes that gate itself, so asking for it while holding a pilot lock would
deadlock against `maintenance_session`. Nothing is deleted and each row only
raises what is already stored, which is what makes the import safe to retry on
every launch.

Voidrunner prints every hotkey as `[K] Label`, without exception (issue #400).
A test must not tell an action bar from a body row by its hotkey style -- there
is only one -- and must not assume a phrase lands on a given page at the 40x12
floor,
because the spaced bar can take a row the glued one did not. The bar is what
`out_prompt` writes: it ends the frame, unterminated, and wraps into as many rows
as the width needs, so its last row is no longer the whole of it. The
`without_action_bar` fixture asks the writer how many rows it took instead of
guessing, and is the right tool whenever a test reassembles a screen's body
across pages.

Voidrunner selection letters come from `choice_letters`, never bare `LETTERS`:
`B` is Back on every screen and must not be a live action, and each list screen
reserves its own hotkeys in its letter map (`MARKET_LETTERS`, `YARD_LETTERS`,
`CREW_LETTERS`, `CHART_CONNECTION_LETTERS`). Adding a hotkey to a list screen
means adding it to that screen's reserved string, not choosing a free-looking
letter. The chart's departure confirmation is the last keystroke; scripted chart
tests that select a connection must answer it, and a constant `read_key` stub
that returns a former hotkey now loops forever instead of failing.

Voidrunner's standalone input decoder reads unbuffered stdin bytes. Mixing
buffered `sys.stdin.buffer.read` with readiness checks can hide prefetched bytes
from the OS and strand a partial key. POSIX uses `select` for partial-key waits;
Windows inherited door pipes need `PeekNamedPipe`, since Windows `select` only
handles sockets. No background reader thread is required. The parser retains
partial UTF-8/CSI/SS3/control-string/paste state across timeouts, limits each call
to 256 bytes, and stores only bounded delimiter/prefix data. After a timeout it
waits for actual data, preventing repeated idle menu redraws. Unsupported-key
sentinels must be nonempty and non-command strings: empty strings match Python's
`key in LETTERS` checks. Text entry filters those sentinels before character
validation. Test the parser with split bytes and timeout events, and exercise
UTF-8 input, standalone Escape, and partial-key EOF through real subprocess pipes.

Voidrunner caches one bounded contract board per galaxy system. Prepare/refresh
it in station checkpoints (never during pending travel), before browsing. The
board reads posted copies and only acceptance checkpoints; returning to the
station skips the outer-loop fallback save. Board generation uses a separate
version-tagged seed (galaxy seed, station, generation day), so preparing offers
never consumes encounter randomness or changes a resumed fight. Generation
allocates each offer ID immediately; acceptance checks unchanged posted terms
and removes the offer in the same checkpoint as adding the active contract.
Never replenish a board merely because its last offer was accepted. ID repair
runs only without pending travel: changing active IDs during an interrupted
journey would break equality with its contract snapshots. Departure expires jobs
before constructing encounter snapshots; completion and resumed legacy bounty/
escort paths also check expiry before consumption, combat, or payment. The
inclusive deadline boundary is `turn > deadline_turn`, not `>=`.


Voidrunner contraband standing follows persisted cash-surplus high-water
milestones. Debit purchases/new futures and credit sales/cancellation principal
through the same ledger; transaction counts, refund fees, or recovering a prior
loss must never mint new milestones. Legacy standing is not recalculated.

Trading acquisition basis is separate from the contraband standing balance.
Only known FIFO lots are stored; hold quantity outside those lots is older,
unknown-cost cargo. Every removal path must consume both cargo and basis in
the same checkpoint: market sales, deliveries, combat dumping, customs,
voluntary jettison and destruction. Futures add their full recorded payment
when collected, never again on repeated settlement. Partial lot cost uses integer
allocation with the remainder retained, so disposing of a whole lot preserves
its exact paid amount. Never infer old cargo cost from current prices. Validate
lot totals against the hold and ledger dates against the career day before load
or persistence. Ledger views are pure; RNG state and checkpoint callbacks must
not change when paging or backing out.

Market memory stores one last-observed buy/sell/day tuple per system/commodity.
Record visible local quotes at docked checkpoints, not while a journey is pending;
trader tips record only their disclosed remote quote with the encounter result.
Neither observation adds RNG calls. Route advice uses saved destination quotes
and pure local procurement/basis calculations, never `price_for` on a remote
system. Keep replacement fuel cost separate from additional cash needed with
fuel already in the tank. Use current crew wages per hop, inclusive arrival-day
contract expiry and actual hold capacity; label unknown basis and infeasible legs.
View state and field selections are transient. Height budgets must include the
wrapped action bar and numbered title, and picker continuations must retain a
selectable key after a long station name crosses a page boundary.

New futures store origin and principal as a pair; absent metadata retains legacy
remote settlement/full-refund behavior. New orders wait for room at their origin.
The departure tick settles only legacy orders; pickup settlement runs after the
arrival position changes and before mission rewards in that same saved phase.
Station-entry settlement also handles mature loaded orders and newly freed holds,
before rescue decides whether the pilot is stranded. Missing legacy pickup keys
stay absent on serialization; explicit nulls are malformed new metadata.
Do not settle by the old origin merely because departure increments the day.
Per-unit integer fee rounding prevents split orders from avoiding the fee.
New orders reserve their quantity from the origin's stock pool through the same
`_consume_market_depth` call the spot path uses and record it in the additive
`reserved` field; cancellation releases it at the origin (not the current
station) through `_release_market_depth`, bounded by the pool ceiling, and
settlement must not touch the pool. Absent `reserved` means an order signed
before reservation existed; the validator rejects `reserved` on legacy orders
and outside `0..quantity`. Any future purchase path that bypasses
`market_depth` reopens the volume exploit that depth was added to close.

Voidrunner contract views must remain read-only, including pagination and Back.
Keep estimates in pure domain helpers; do not query hidden remote market prices
or mark bearings discovered. Missing-cargo/current-fuel cash estimates exclude
sunk costs and must not be labelled total profit. Acceptance is a distinct action
on the final details page; tracking and confirmed abandonment checkpoint before
acknowledgement. Clear the tracked ID when its active contract disappears, and
reject contract-management actions during pending travel so snapshots remain valid.
Tracking cleanup itself runs at every checkpoint, including pending mission
removals. An active bounty at the current station needs an outbound/return pair
to trigger again; its budget and chart hint must use that same route. Chart
tracking must name the actual displayed connection key, including uncharted legs.
Later same-target bounties budget earlier contracts and their re-entry legs.
Contract list pages reserve the actual wrapped footer/header height and split
oversized entries into selectable continuations; row-count tests run at the 40x12 floor.
A wrapped entry shows its hotkey once: `keyed_rows` prefixes the first row and
indents continuation rows by the prefix width (a continuation that opens a new
page carries the key again so that page stays selectable); do not re-key every
row, which reads as duplicate entries (issue #411).
Scripted acceptance tests must navigate the details pages before pressing A;
otherwise they no longer exercise acceptance. Test compact screens by counting
display columns and physical rows between successive input requests.

One-page screens strip the Prev/Next tokens through `single_page_footer` but keep
the `N/M` page counter: dozens of scripted tests (and readers) page by that
counter, so it is not decoration to remove (issue #412).

The detail action bar labels each command and Back explicitly, including when
its available commands vary by stage (A means Accept before a faction case and
Aid after investigation). Reserve the complete wrapped action bar in page
capacity. Full-content UI tests remove that bar as a unit before rejoining prose;
filtering only the old Act row leaves wrapped footer fragments inside the body.

Voidrunner achievement scores project current metrics and all retained dossiers
from the saved career. Build every category from an untrimmed score snapshot;
cutting to the wealth top 20 before selecting combat/trading/exploration loses
qualifying records. Keep legacy aggregate wealth and retirement counts separate
from versioned per-career metrics. Retry optional score projection from dossiers
after restart, including a failure on the retirement checkpoint itself. Future
summary versions and extra fields are preserved. An unchanged projection must skip
replacement: identical file bytes alone do not prove a view was read-only. Validate
its types before equality-based no-op detection because Python equates False with
zero. Inspect
actual replacements after input, excluding the ordinary EOF cleanup checkpoint.
Score reads share the existing
4 MiB career/backup per-file ceiling; the summary has at most 129 career entries.

Voidrunner's box family (`_box_title`, `_pad`, the title splash and the
registration box) measures with `_visible_width`, never `len()` after an ANSI
strip: a callsign may contain East Asian wide characters, which are two columns
each. `title_rows` yields the large composition only when the 49-column logo
fits and otherwise a complete compact one (wordmark, wrapped subtitle, stacked
meta fields); no row may exceed `_box_inner_width()`, and tests assert one
display width across every box row at 80, 48 and 40 columns.

Voidrunner portraits paginate an entire authored composition as one group.
Choose a complete compact version when either width or height rules out the
large version; omit artwork only if even the compact composition cannot fit.
Sanitize and wrap dynamic details before styling the trusted art. Keep action
results and landmark salvage status ahead of portraits so small terminals do
not displace the outcome. Commissioning tests must select the preview's C action
before answering its final confirmation; a lone Y no longer purchases a hull.

Viewport pictures (`show_viewport_picture`) are not scenes: the key that ends
the idle is returned as the command, never drained, and the bottom row is the
action bar's, so a picture never draws on it and every idle frame moves the
cursor back to the end of the bar. A picture that does not `fit` -- too little
sky left above its caption -- falls back to the ASCII portrait for that view
only. Keep idle effects cheap: a moving element should change the cells it
leaves and enters, not shift a gradient across the cells it covers (the drive
plume's gradient is anchored to the nozzle for that reason). Opening plus the
whole idle is asserted under 100 KB at 80x24 in truecolor.

In-flight scenes (`play_scene`) run after the commit they illustrate; a call
placed before a commit would let an interrupted session show an outcome that
was never kept. The contact scene belongs only to a fight whose combat slot was
just created, and the outcome scenes only to the exchange that set the outcome;
the cached-outcome return at the top of `_screen_combat_session` must stay free
of them, or a resumed hop replays its explosion. `play_scene` takes a factory
and builds the scene only once it will play, so scripted sessions and `fast`
callers pay nothing. Every subprocess test and every gallery walk runs with
`DOOR_SCENES=0`: a scene takes the key that ends it, and a walk typed one key
at a time would lose keystrokes to it. Scene randomness is a private
`random.Random` seeded from text, never the event or galaxy generators. Wire
size is asserted per scene at 80x24; a new effect that repaints the whole sky
each frame needs one flat color (as the jump's flash does) or stepped values,
not a per-pixel blend.

Accolades (achievements) are captured in `World.commit` beside the rank: the
unlock is part of the document being written, and `pending_accolades` is
extended only after the write succeeds. A counter in `ACCOLADE_COUNTERS` must be
bumped beside the effect it counts and before that effect's commit -- inside
the block that sets a combat outcome, the encounter result, the bounty payout --
so a resumed hop, which returns a kept outcome, cannot count it twice. A new
destruction, rescue or customs path must bump the same counter the existing
one does. The record is additive and omitted while empty (a Shuttle is implied,
not recorded), which keeps a fresh career's document byte-identical to what it
was; tests that compare saved bytes across a door launch must start from a
career whose unlocks are already on file (`unlock_accolades` before `persist`),
since the launch tick legitimately records retroactive ones. Ids this build
does not know are preserved, never refused: a pilot who plays on an older node
keeps what a newer one awarded. `retire_pilot` copies the record and the
finale screen hands the fresh world's `pending_accolades` across, exactly as it
does promotions; without that hand-off the retirement achievements are earned
silently.

NetBBS game events (door API 5, #25) are reported by `GameEvents`, which
exists only when `door_info.json` lists `outbound.events` and names a positive
`user_id`. It is bound to the world after the launch checkpoint, never before:
that tick is where a career first opened by a newer build records retroactive
accolades, and announcing them would flood the channel with history. It is
told about a commit only after the save succeeded (`World.report_earned`), and
the finale screen reports the fresh world's earnings explicitly, because the
fresh world that found them has no reporter. Bronze accolades are not sent,
nor a rank's own accolade when its promotion is. Standings are the Hall's
wealth view, recomputed only when this pilot's best credits or callsign moved,
and sent when the rows differ from the last snapshot. Every request is written
atomically under a zero-padded name, since NetBBS answers in name order; a
request that cannot be written is dropped, never raised.

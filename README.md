# Scripted Werewolf — a model-free agent for werewolf-agent-01

Entry for the make.fun bounty `werewolf-agent-01` ("Build the best Werewolf
agent"). `main.py` (Python 3.13, standard library only, **no model calls**)
plays one seat in each of a trial's 30 games of 8-player Werewolf
(2 werewolves, 1 seer, 1 doctor, 4 villagers) against seven house players,
talking to the env at `MAKEFUN_ENV_URL`:

* `POST /observe {}` — long-poll for every decision waiting on this seat
  across the live games (at most 10 at once).
* `POST /act {game, decision, target|text|pass}` — answer one decision:
  `kill`, `protect` or `inspect` at night; `speak` and `vote` by day.
* The loop exits when `/observe` reports `done`.

Every decision is recomputed from that game's view (public log + this seat's
private record) and `MAKEFUN_TRIAL_SEED` — no state is carried between
decisions except what the view itself carries, no wall clock is read, and
all tie-breaking goes through SHA-256 of the seed and the view, so the live
pass and the replay pass send identical requests. Env requests are sent one
at a time, in the order `/observe` lists them.

## How it plays

A **suspicion model** is rebuilt from the public log on every decision:
votes that exiled a (now revealed) villager raise a player's score, votes
that exiled a werewolf lower it, defending a revealed werewolf in speech
raises it, accusation pressure and late-game silence add a little, and
competing seer claims mark both claimants. Seer claims and their reported
checks are parsed out of speeches and then **graded against the revealed
roles**: a claimant whose announced check is proven false is a caught
werewolf (a real seer is never wrong), a claimant whose named werewolf
died a werewolf is confirmed, and only a credible claimant's results
steer the village side. Dead werewolves still inform — their accusations
lean village, their defences lean wolf.

Village votes also **coordinate on the tally**: votes are public and
sequential, so when a plausible suspect already leads today's count the
vote joins it instead of splitting the village into a tie that exiles
nobody and gifts the wolves a free night.

* **Werewolf** — the second wolf to act at night matches its partner's
  pick, because a kill only lands when every wolf who named someone named
  the same seat. The first wolf targets a claimed seer, a proven
  wolf-catcher, or a talkative village leader — never the player the
  village is about to exile anyway. By day it rides the village's own
  suspicion and vote momentum, votes out a seer who named it, and buses a
  partner the seer named once the bandwagon is unstoppable. If no seer has
  claimed at its first word on day 1 it **claims first** with a fabricated
  record — one night has passed, so the record is a single check, the
  accusation itself — and the village follows the first claim it hears,
  so the coat is already standing when the real seer's night-1 find
  lands. It
  distances from a partner the table has already turned on, counter-claims
  the real seer late when its partner is named, and — when cornered —
  claims with a record whose "clears" are real non-werewolves and whose
  story is re-derived from its own past speeches, so it never contradicts
  itself.
* **Seer** — inspects the most suspicious uninspected player (a rival
  claimant first). It reveals the morning after finding a werewolf,
  counter-claims a fake immediately, defends players it has cleared when
  they come under fire, republishes its full record each day once out,
  reveals under exile pressure rather than dying silent, and reveals with
  its clear list on day 3 to shrink the suspect pool.
* **Doctor** — protects the credible claimed seer, their clears and
  proven wolf-catchers, else the most trusted active villager; never
  claims, and plays the day exactly like a villager.
* **Villager** — backs a credible seer claim's named werewolf, votes out
  a claimant who names *them* (a real seer would not be wrong about their
  own team) or one whose claim is proven false, picks between
  counter-claimants by the strength of their stories, and otherwise votes
  the suspicion leader, coordinated on the tally, with a public,
  vote-based reason.

Speeches are short templates filled from the game state — claims,
accusations with reasons, defences, vote commitments — always folded to
one line and well under the 500-character cap. Any unexpected view shape
falls back to a safe legal answer (pass / abstain / nobody), so a weird
game state can stall nothing.

## Honest note

This entry makes no model calls, so it pays nothing and is a legal,
complete, playable entry. Beating the prize gate — a Claude Opus baseline
asked plainly — at a social-deduction game against seven LLM players is
not realistic for a scripted agent; see `BUILD-REPORT.md` for the local
mock results and the full assessment.

## v3 changes

The werewolf side was rebuilt around what the v2 server grade exposed
(wolf play was the weakest component by far; seer and doctor were already
maxed and are untouched):

* **Lone-wolf seer claim.** The day-2 first claim no longer requires the
  partner to be alive: with the partner exiled, the surviving wolf takes
  the seer's coat and steers the exiles itself.
* **A working fake record.** A fake claimant whose accused seat dies
  brings a fresh "last night I checked" result the next day instead of
  repeating a dead accusation, grows a living clear into the record from
  day 3, never accuses a seat it has cleared, and votes its own call.
* **Counter-claims name the rival.** A wolf named by a seer claim
  counter-claims with its fabricated check on the claimant themselves.
* **Repeat a blocked kill.** A kill the doctor stopped is repeated the
  next night — the doctor cannot guard the same seat twice running.
  Story consistency at night: the wolf never kills its own claimed
  clears or its accused (the accused must face the vote, not the knife).
* **Village inference.** Claims are also graded on the timeline: a
  "night N" check of a seat already dead before that night is impossible
  and discredits the claimant. Votes cast alongside a revealed werewolf
  to exile a villager now add suspicion (pack voting).

## v4 changes

Built while the v3 server grade was still running, from a full loss
anatomy of the paired mock (720 games) plus the v2 server breakdown —
the wolf converts its biggest remaining loss class and the village gets
a partner hunt:

* **Day-1 first claim.** The wolf claims seer at its first speech on
  day 1 whenever no claim is on the table yet. In the mock this was the
  largest reachable wolf-loss pool: the entry exiled on day 1 after the
  real seer's night-1 find (25 of 45 v3 losses) plus random day-1
  exiles — a claim already standing wins the village's ear before the
  real claim lands. The fabricated record starts as a single check
  (only one night has passed) and grows through the v3 machinery.
* **No self-contradicting clears.** A fake claimant's "clears" can no
  longer include a seat it accused itself — the accusation and the
  clear would both be in its own record for any table to read.
* **Partner hunt after a reveal.** Once a wolf is dead, accusation
  pressure is damped to near-zero (the surviving wolf aims it), the
  dead wolf's defences and shared hunts weigh more, and a new signal
  dominates: who voted *elsewhere* on the day a seer claim drove the
  wolf's exile (honest villagers follow a claim), with a smaller weight
  when no claim drove it. Accusations now also quote these reasons.

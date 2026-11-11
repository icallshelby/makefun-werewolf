#!/usr/bin/env python3
"""make.fun werewolf-agent-01 entry: a scripted, model-free Werewolf player.

Pure Python 3 standard library. The agent talks to the trial env at
MAKEFUN_ENV_URL (POST /observe, /game, /act) and plays one seat in each of
the trial's 30 games of 8-player Werewolf (2 werewolves, 1 seer, 1 doctor,
4 villagers) against seven house players.

No model calls are made: every decision is a deterministic function of the
game's view (public log + this seat's private record) and MAKEFUN_TRIAL_SEED.
There is no wall-clock use and no unseeded randomness, so the live pass and
the replay pass send byte-identical requests. Env requests are sent one at a
time, in the order /observe lists them.

Strategy summary (see README.md for the full version):
  * A suspicion model is rebuilt from the public log on every decision:
    votes for/against players whose roles are now revealed, seer claims and
    counter-claims parsed from speeches, defence of revealed werewolves,
    speech volume, and today's votes already cast.
  * Claims are graded against the revealed roles: a claimant whose
    announced check is proven false is a caught werewolf (a real seer is
    never wrong), a claimant whose named werewolf died a werewolf is
    confirmed, and only a credible claimant's results steer the village.
    Dead werewolves still inform: their accusations lean village, their
    defences lean wolf.
  * Village votes coordinate: votes are public and sequential, so when a
    plausible suspect already leads today's tally the vote joins it rather
    than splitting the village into a tie that exiles nobody.
  * Werewolf: coordinate the night kill with the partner (the second wolf
    matches the first's pick), kill claimed seers and village leaders, push
    day votes onto villagers the village already suspects, bus a doomed
    partner, distance from a partner the table has already turned on,
    counter-claim the real seer late, and fake-claim seer when cornered
    (fabricated checks are derived from the game state, so they stay
    consistent all game).
  * Seer: inspect the most suspicious uninspected player (a rival claimant
    first), reveal on the day after finding a werewolf, counter-claim a
    fake, defend cleared players, republish the full record once out, and
    reveal under exile pressure rather than die silent.
  * Doctor: protect the credible claimed seer, their clears and proven
    wolf-catchers, else the most trusted village leader; never claim.
  * Villager: follow the credible seer claim, vote out a claimant who
    names you (they are lying) or one whose claim is proven false,
    otherwise vote the suspicion leader, coordinated on the tally.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Env access
# --------------------------------------------------------------------------

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _env_url():
    return (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")


def _trial_seed():
    return os.environ.get("MAKEFUN_TRIAL_SEED") or "makefun-werewolf"


def env_post(path, payload, timeout=120):
    """One POST to the env; returns (status, parsed_json_or_None)."""
    url = _env_url() + path
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, (json.loads(raw) if raw else {})
        except ValueError:
            return err.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


# --------------------------------------------------------------------------
# Deterministic helpers (hashlib only: no PYTHONHASHSEED dependence)
# --------------------------------------------------------------------------

def det_float(*parts):
    """A deterministic float in [0, 1) from the trial seed and the parts."""
    h = hashlib.sha256()
    h.update(_trial_seed().encode("utf-8"))
    for p in parts:
        h.update(b"|")
        h.update(str(p).encode("utf-8"))
    return int.from_bytes(h.digest()[:8], "big") / float(1 << 64)


def det_pick(items, *parts):
    """Deterministically pick one item from a non-empty list."""
    if not items:
        return None
    idx = int(det_float(*parts) * len(items)) % len(items)
    return items[idx]


def argmax_key(items, keyfn):
    """Item with the highest keyfn(item); the earliest item wins ties.

    Shared by every "pick the best-scoring seat" decision below: they all
    used to open-code this same scan, so the tie rule (strict comparison,
    first maximum kept) now lives in exactly one place.
    """
    best, best_key = None, None
    for item in items:
        key = keyfn(item)
        if best_key is None or key > best_key:
            best, best_key = item, key
    return best


def fold(text):
    """Fold a statement the env's way and keep it safely under the cap."""
    text = re.sub(r"[\t\n\v\f\r\u0085\u2028\u2029]+", " ", text)
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    if len(text) > 480:
        text = text[:480].rsplit(" ", 1)[0].strip()
    return text


# --------------------------------------------------------------------------
# View analysis: everything is re-derived from the view on each decision,
# so a decision depends only on (view, trial seed) -- replay-safe.
# --------------------------------------------------------------------------

SEER_CLAIM_RE = re.compile(
    r"\bi(?:'m| am) (?:the )?seer\b|\bi am a seer\b|\bas your seer\b", re.I)
DOCTOR_CLAIM_RE = re.compile(r"\bi(?:'m| am) (?:the )?doctor\b", re.I)
WOLF_RESULT_RES = [
    re.compile(r"seat\s*(\d)\s*(?:is|:|=)\s*(?:a\s+)?werewolf", re.I),
    re.compile(r"seat\s*(\d)\s*is\s*a\s+wolf\b", re.I),
    re.compile(r"checked\s*seat\s*(\d)\s*[:\-]?\s*(?:a\s+)?werewolf", re.I),
    re.compile(r"seat\s*(\d)\s*came back\s*(?:a\s+)?werewolf", re.I),
    re.compile(r"seat\s*(\d)\s*[:\-]\s*werewolf\b", re.I),
]
CLEAR_RESULT_RES = [
    re.compile(r"seat\s*(\d)\s*is\s*not\s*a?\s*werewolf", re.I),
    re.compile(r"seat\s*(\d)\s*is\s*(?:innocent|a villager|village|clean|town)\b", re.I),
    re.compile(r"checked\s*seat\s*(\d)\s*[:\-]?\s*(?:not|innocent|clean|villager)", re.I),
    re.compile(r"seat\s*(\d)\s*came back\s*(?:clean|innocent|not)", re.I),
]
ACCUSE_CUES = ("suspect", "suspicious", "wolf", "lying", "liar", "quiet",
               "evasive", "accuse", "against", "vote out", "exile")
TRUST_CUES = ("trust", "believe", "innocent", "clear", "good")


class GameState:
    """Everything the agent knows about one game, parsed from one view."""

    def __init__(self, view):
        self.view = view
        self.day = int(view.get("day") or 1)
        self.phase = view.get("phase") or ""
        you = view.get("you") or {}
        self.me = you.get("seat")
        self.my_name = you.get("name") or ""
        self.role = you.get("role") or ""
        partner = you.get("partner") or {}
        self.partner = partner.get("seat") if partner else None
        self.players = {}
        for p in view.get("players") or []:
            self.players[p.get("seat")] = {
                "name": p.get("name") or "",
                "alive": bool(p.get("alive")),
                "role": p.get("role"),  # None until revealed by death
            }
        self.log = view.get("log") or []
        self.speeches = []       # (day, seat, text)
        self.passes = []         # (day, seat)
        self.votes = []          # (day, seat, target)
        self.exiles = []         # (day, seat, role)
        self.dawns = []          # (day, died, role)
        for e in self.log:
            kind = e.get("kind")
            if kind == "speech":
                self.speeches.append((e.get("day"), e.get("seat"), e.get("text") or ""))
            elif kind == "pass":
                self.passes.append((e.get("day"), e.get("seat")))
            elif kind == "vote":
                self.votes.append((e.get("day"), e.get("seat"), e.get("target")))
            elif kind == "exile":
                self.exiles.append((e.get("day"), e.get("seat"), e.get("role")))
            elif kind == "dawn":
                self.dawns.append((e.get("day"), e.get("died"), e.get("role")))
        # Private records
        self.seer_results = view.get("seerResults") or []   # seer only
        self.last_protect = view.get("lastProtect")          # doctor only
        self.wolf_votes = view.get("wolfVotes") or []        # werewolf only
        # Claims parsed from speeches: speaker seat -> claim record.
        # claim_events keeps the (day, claimant, target, verdict) sequence so
        # later logic can judge a claim against what death revealed, and
        # claim_first_day records when each claim was first made (a claim
        # that only appears after a real seer reveals is suspect by shape).
        self.seer_claims = {}
        self.claim_events = []
        self.claim_first_day = {}
        self.doctor_claimants = set()
        for (_day, seat, text) in self.speeches:
            if SEER_CLAIM_RE.search(text):
                rec = self.seer_claims.setdefault(
                    seat, {"wolf": set(), "clear": set()})
                self.claim_first_day.setdefault(seat, _day)
                for rx in WOLF_RESULT_RES:
                    for m in rx.finditer(text):
                        tgt = int(m.group(1))
                        if tgt not in rec["wolf"]:
                            rec["wolf"].add(tgt)
                            self.claim_events.append((_day, seat, tgt, "wolf"))
                for rx in CLEAR_RESULT_RES:
                    for m in rx.finditer(text):
                        tgt = int(m.group(1))
                        if tgt not in rec["clear"]:
                            rec["clear"].add(tgt)
                            self.claim_events.append((_day, seat, tgt, "clear"))
            if DOCTOR_CLAIM_RE.search(text):
                self.doctor_claimants.add(seat)

    # -- basic helpers -----------------------------------------------------
    def alive(self, seat):
        p = self.players.get(seat)
        return bool(p and p["alive"])

    def living(self):
        return sorted(s for s, p in self.players.items() if p["alive"])

    def revealed(self, seat):
        p = self.players.get(seat)
        return p["role"] if p else None

    def name(self, seat):
        p = self.players.get(seat)
        return p["name"] if p else "?"

    def label(self, seat):
        return "seat %d (%s)" % (seat, self.name(seat))

    def speech_count(self, seat):
        return sum(1 for (_d, s, _t) in self.speeches if s == seat)

    def my_speeches_today(self):
        return sum(1 for (d, s, _t) in self.speeches
                   if s == self.me and d == self.day)

    def votes_today(self):
        return [(s, t) for (d, s, t) in self.votes if d == self.day]

    def votes_against_today(self, seat):
        return sum(1 for (_s, t) in self.votes_today() if t == seat)

    def accusations_against(self, seat):
        """How many speeches accuse this seat with a negative cue."""
        n = 0
        pat = re.compile(r"seat\s*%d\b" % seat)
        for (_d, s, text) in self.speeches:
            if s == seat:
                continue
            low = text.lower()
            if pat.search(low) and any(c in low for c in ACCUSE_CUES):
                n += 1
        return n

    def i_claimed_seer(self):
        return any(SEER_CLAIM_RE.search(t)
                   for (_d, s, t) in self.speeches if s == self.me)

    def my_claimed_results(self):
        rec = self.seer_claims.get(self.me) or {"wolf": set(), "clear": set()}
        return rec

    def other_seer_claimants(self):
        return sorted(s for s in self.seer_claims if s != self.me and self.alive(s))

    # -- suspicion ---------------------------------------------------------
    def suspicion(self):
        """Scores for living players (higher = more likely a werewolf).

        Built only from public information: revealed roles, the votes that
        produced them, claim structure, and speech patterns.
        """
        scores = {s: 0.0 for s in self.living()}
        revealed = {s: p["role"] for s, p in self.players.items() if p["role"]}
        wolves_dead = {s for s, r in revealed.items() if r == "werewolf"}
        # Voting record against revealed outcomes.
        exile_role = {}
        for (d, seat, role) in self.exiles:
            if seat is not None:
                exile_role[(d, seat)] = role
        for (d, voter, target) in self.votes:
            if target is None:
                continue
            role = exile_role.get((d, target)) or revealed.get(target)
            if target is not None and role == "villager":
                if voter in scores:
                    scores[voter] += 1.0   # helped exile a villager
            if target is not None and role == "werewolf":
                if voter in scores:
                    scores[voter] -= 1.2   # helped exile a werewolf
            if voter in wolves_dead and target in scores:
                scores[target] -= 0.4      # a known wolf voted them: village-lean
        # Defending a revealed wolf in speech.
        for (_d, s, text) in self.speeches:
            if s not in scores:
                continue
            low = text.lower()
            if any(c in low for c in TRUST_CUES):
                for w in wolves_dead:
                    if re.search(r"seat\s*%d\b" % w, low):
                        scores[s] += 0.8
        # Accusation pressure (weak signal; herd often right in this game).
        for s in list(scores):
            scores[s] += 0.35 * self.accusations_against(s)
        # Quiet players drift up slightly as days pass.
        if self.day >= 2:
            for s in list(scores):
                c = self.speech_count(s)
                if c == 0:
                    scores[s] += 0.5
                elif c <= 1:
                    scores[s] += 0.2
        # A revealed werewolf's own words still carry information: wolves
        # accuse villagers and shield their partner, so their accusations
        # lean village and their defences lean wolf.
        for (_d, s, text) in self.speeches:
            if s not in wolves_dead:
                continue
            low = text.lower()
            accused = any(c in low for c in ACCUSE_CUES)
            trusted = any(c in low for c in TRUST_CUES)
            for t in list(scores):
                if re.search(r"seat\s*%d\b" % t, low):
                    if accused:
                        scores[t] -= 0.2
                    if trusted:
                        scores[t] += 0.5
        # Players who accused a revealed wolf before it died were hunting.
        for w in wolves_dead:
            pat = re.compile(r"seat\s*%d\b" % w)
            for (_d, s, text) in self.speeches:
                if s in scores and s != w:
                    low = text.lower()
                    if pat.search(low) and any(c in low for c in ACCUSE_CUES):
                        scores[s] -= 0.3
        # Claim structure.
        claimants = [s for s in self.seer_claims if self.alive(s)]
        if len(claimants) >= 2:
            for c in claimants:
                if c in scores:
                    scores[c] += 0.8       # one of the claimants is lying
        # Proven claim lies are the strongest public tell there is: the
        # claimant is a werewolf wearing the seer's coat.
        status = self.claim_status()
        for c, state in status.items():
            if c in scores and state == "discredited":
                scores[c] += 2.5
            if c in scores and state == "confirmed":
                scores[c] -= 1.5
        # A claimant who calls the same seat both wolf and clear is
        # improvising, not reporting.
        for c, rec in self.seer_claims.items():
            if c in scores and (rec["wolf"] & rec["clear"]):
                scores[c] += 1.5
        return scores

    def claim_analysis(self):
        """Seer claims from the village's point of view.

        Returns (lone_claimant, named_wolves, named_clears): when exactly one
        living player other than me claims seer and I am not the seer, their
        reported results are the village's best lead.
        """
        others = [s for s in self.other_seer_claimants()]
        if self.role == "seer" or len(others) != 1:
            return None, set(), set()
        rec = self.seer_claims.get(others[0]) or {"wolf": set(), "clear": set()}
        wolves = {s for s in rec["wolf"] if self.alive(s)}
        clears = {s for s in rec["clear"] if self.alive(s)}
        return others[0], wolves, clears

    def claim_status(self):
        """Grade every seer claimant against what the deaths have revealed.

        A claimant is "discredited" once any check they announced is proven
        false by a revealed role (their "werewolf" was village-side, or
        their "clear" was a werewolf): a real seer cannot be wrong, so a
        discredited claimant is a werewolf in a costume. They are
        "confirmed" once a werewolf they named was revealed as one. Anyone
        else is "open". Returns {seat: status} for living claimants.
        """
        status = {}
        for seat, rec in self.seer_claims.items():
            if not self.alive(seat):
                continue
            state = "open"
            for tgt in rec["wolf"]:
                role = self.revealed(tgt)
                if role == "werewolf":
                    state = "confirmed"
                elif role is not None:
                    state = "discredited"
                    break
            if state != "discredited":
                for tgt in rec["clear"]:
                    if self.revealed(tgt) == "werewolf":
                        state = "discredited"
                        break
            status[seat] = state
        return status

    def credible_claimant(self):
        """The one living claimant (not me) the village should believe.

        A lone claimant who is not discredited is credible. With rivals,
        only a confirmed claimant is: their accusation already produced a
        dead werewolf. Otherwise the claim fight is unresolved and None is
        returned. As the seer I believe nobody else's claim, ever.
        """
        if self.role == "seer":
            return None
        status = self.claim_status()
        live = [s for s in status if s != self.me]
        believable = [s for s in live if status[s] != "discredited"]
        if len(live) == 1 and believable:
            return believable[0]
        confirmed = [s for s in believable if status[s] == "confirmed"]
        if len(confirmed) == 1:
            return confirmed[0]
        return None

    def claimant_clears(self, claimant):
        """Living players a claimant has cleared, as a set of seats."""
        rec = self.seer_claims.get(claimant) or {"wolf": set(), "clear": set()}
        return {s for s in rec["clear"] if self.alive(s)}


# --------------------------------------------------------------------------
# Night decisions
# --------------------------------------------------------------------------

def decide_kill(st, options):
    opts = [o for o in options if o != st.me and o != st.partner]
    if not opts:
        return None
    # Second wolf to act: match the partner's pick so the kill lands
    # (a kill needs every wolf who named someone to name the same seat).
    for v in st.wolf_votes:
        if v.get("night") == st.day and v.get("seat") == st.partner:
            t = v.get("target")
            if t in opts:
                return t
    scores = st.suspicion()
    best, best_key = None, None
    for s in opts:
        key = 0.0
        if s in st.seer_claims:
            key += 6.0                    # a claimed seer is the top target
        key += 0.55 * st.speech_count(s)  # talkative village leaders
        key += 0.9 * sum(1 for (_d, v, t) in st.votes
                         if t is not None and st.revealed(t) == "werewolf"
                         and v == s)      # players who caught a wolf
        key -= 0.8 * scores.get(s, 0.0)   # don't kill who the village will exile
        key += det_float("kill", st.day, s, len(st.log))
        if best_key is None or key > best_key:
            best, best_key = s, key
    return best


def decide_inspect(st, options):
    inspected = {r.get("seat") for r in st.seer_results}
    opts = [o for o in options if o != st.me]
    if not opts:
        return None
    fresh = [o for o in opts if o not in inspected] or opts
    scores = st.suspicion()
    best, best_key = None, None
    for s in fresh:
        key = scores.get(s, 0.0)
        if s in st.seer_claims:
            key += 5.0                    # verify a rival claimant first
        key += det_float("inspect", st.day, s, len(st.log))
        if best_key is None or key > best_key:
            best, best_key = s, key
    return best


def decide_protect(st, options):
    opts = [o for o in options if o != st.me]
    if not opts:
        return None
    claimant, _w, _c = st.claim_analysis()
    credible = st.credible_claimant()
    if credible is not None:
        claimant = credible
    clears = st.claimant_clears(claimant) if claimant is not None else set()
    # Players who voted a revealed werewolf out are proven hunters: the
    # wolves revenge-kill them and the village cannot spare their votes.
    catchers = {v for (_d, v, t) in st.votes
                if t is not None and st.revealed(t) == "werewolf"}
    scores = st.suspicion()
    best, best_key = None, None
    for s in opts:
        key = 0.0
        if claimant is not None and s == claimant:
            key += 7.0                    # keep the seer alive
        elif s in st.seer_claims:
            key += 2.0
        if s in clears:
            key += 2.0                    # a confirmed clear is a key vote
        if s in catchers:
            key += 1.2
        key += 0.4 * st.speech_count(s)
        key -= 0.7 * scores.get(s, 0.0)   # wolves rarely kill the suspected
        key += det_float("protect", st.day, s, len(st.log))
        if best_key is None or key > best_key:
            best, best_key = s, key
    return best


# --------------------------------------------------------------------------
# Day votes
# --------------------------------------------------------------------------

def _suspicion_leader(st, options, exclude):
    scores = st.suspicion()
    pool = [s for s in options if s not in exclude]
    return argmax_key(pool, lambda s: scores.get(s, 0.0)
                      + det_float("vote", st.day, s, len(st.log)))


def _bandwagon(st, target, trusted_clear):
    """Coordinate on one exile: votes are public and sequential, so a
    village that splits its votes exiles nobody and gifts the wolves a
    free night. If another plausible suspect is already ahead in today's
    tally, join them instead of splitting — unless that leader is someone
    we trust clear. In the endgame the bar for joining drops: an exile is
    the village's only weapon left.
    """
    if target is None:
        return None
    tally = {}
    for (_s, t) in st.votes_today():
        if t is not None:
            tally[t] = tally.get(t, 0) + 1
    if tally.get(target, 0) >= 1:
        return target
    scores = st.suspicion()
    endgame = len(st.living()) <= 5
    need = 1 if endgame else 2
    margin = 1.0 if endgame else 0.75
    leader = argmax_key(list(tally), lambda s: tally[s])
    if (leader is not None and leader != target and leader in st.living()
            and leader not in trusted_clear
            and tally.get(leader, 0) >= need
            and scores.get(leader, 0.0) >= scores.get(target, 0.0) - margin):
        return leader
    return target


def decide_vote_village(st, options):
    opts = [o for o in options if o != st.me]
    if not opts:
        return None
    known_wolves = set()
    known_clear = set()
    if st.role == "seer":
        for r in st.seer_results:
            if r.get("werewolf"):
                known_wolves.add(r.get("seat"))
            elif r.get("seat") is not None:
                known_clear.add(r.get("seat"))
    for s in sorted(known_wolves):
        if s in opts and st.alive(s):
            return s
    # Any claimant who names ME as a werewolf is lying: whatever my role
    # on the village side, I know I am not one. Vote the liar out.
    for c in st.other_seer_claimants():
        rec = st.seer_claims.get(c) or {}
        if st.me in rec.get("wolf", set()) and c in opts:
            return c
    rivals = st.other_seer_claimants()
    status = st.claim_status()
    if st.role == "seer":
        # Every rival is lying. A discredited rival is the easiest sell.
        fake = [c for c in rivals if status.get(c) == "discredited"]
        pool = fake or rivals
        if pool:
            pick = pool[0] if pool[0] in opts else None
            return pick or _suspicion_leader(st, opts, known_clear)
    # A discredited claimant is a caught wolf: the best exile on the table.
    caught = [c for c in rivals if status.get(c) == "discredited" and c in opts]
    if caught:
        scores = st.suspicion()
        return argmax_key(caught, lambda s: scores.get(s, 0.0)
                          + det_float("vote", st.day, s, len(st.log)))
    # Follow the credible claimant's named werewolf; trust their clears.
    credible = st.credible_claimant()
    if credible is not None:
        rec = st.seer_claims.get(credible) or {"wolf": set(), "clear": set()}
        for s in sorted(rec["wolf"]):
            if s in opts and st.alive(s):
                return s
        known_clear |= st.claimant_clears(credible)
    elif len(rivals) >= 2:
        # Unresolved claim fight: one claimant is a wolf. Prefer exiling
        # the claimant whose story is weaker — an earlier claim that has
        # produced a confirmed wolf is strong; between open claims, the
        # later one is the wolf's usual counter-claim.
        open_rivals = [c for c in rivals if c in opts]
        if open_rivals:
            latest = max(st.claim_first_day.get(c) or 0 for c in open_rivals)

            def weakness(c):
                key = 0.0
                if status.get(c) == "confirmed":
                    key -= 3.0
                if (st.claim_first_day.get(c) or 0) >= latest and latest > 0:
                    key += 0.5    # the counter-claim usually comes second
                return key
            worst = argmax_key(open_rivals, weakness)
            if worst is not None:
                return worst
    target = _suspicion_leader(st, opts, known_clear)
    return _bandwagon(st, target, known_clear)


def decide_vote_wolf(st, options):
    opts = [o for o in options if o != st.me]
    if not opts:
        return None
    partner = st.partner
    today = st.votes_today()
    tally = {}
    for (_s, t) in today:
        if t is not None:
            tally[t] = tally.get(t, 0) + 1
    claimant, named_wolves, _c = st.claim_analysis()
    # A seer named me: vote the claimant to fight the claim.
    if claimant is not None and st.me in named_wolves and claimant in opts:
        return claimant
    # Partner named by a seer and the bandwagon is already rolling: bus them.
    if partner is not None and partner in opts:
        if partner in named_wolves and tally.get(partner, 0) >= 2:
            return partner
    # Otherwise ride the village's momentum against a non-wolf.
    scores = st.suspicion()
    best, best_key = None, None
    for s in opts:
        if s == partner:
            continue
        key = 2.0 * tally.get(s, 0) + 0.6 * scores.get(s, 0.0)
        key += det_float("wolfvote", st.day, s, len(st.log))
        if best_key is None or key > best_key:
            best, best_key = s, key
    if best is not None:
        return best
    return opts[0]


def decide_vote(st, options):
    if st.role == "werewolf":
        return decide_vote_wolf(st, options)
    return decide_vote_village(st, options)


# --------------------------------------------------------------------------
# Day speeches
# --------------------------------------------------------------------------

def _top_suspect(st, exclude):
    scores = st.suspicion()
    pool = [s for s in st.living() if s != st.me and s not in exclude]
    return argmax_key(pool, lambda s: scores.get(s, 0.0)
                      + det_float("suspect", st.day, s, len(st.log)))


def _reason_for(st, seat):
    """A short public reason to suspect a seat, from the voting record."""
    for (d, seat_ex, role) in reversed(st.exiles):
        if role == "villager":
            voters = [v for (vd, v, t) in st.votes if vd == d and t == seat_ex]
            if seat in voters:
                return "helped exile %s, who was a villager" % st.label(seat_ex)
    if st.speech_count(seat) == 0 and st.day >= 2:
        return "has said almost nothing all game"
    if st.accusations_against(seat) >= 2:
        return "several players have flagged them"
    return "their voting pattern does not add up"


def _defence(st):
    return ("I am village, and my votes show it. Look at who is pushing "
            "this and ask what they gain if I go.")


def speech_seer(st):
    results = st.seer_results
    found = [r for r in results if r.get("werewolf") and st.alive(r.get("seat"))]
    clears = [r for r in results if not r.get("werewolf")]
    claimed = st.i_claimed_seer()
    rivals = st.other_seer_claimants()

    def results_text():
        parts = []
        for r in sorted(results, key=lambda r: r.get("night") or 0):
            verdict = "a werewolf" if r.get("werewolf") else "not a werewolf"
            parts.append("night %d I checked seat %d: %s"
                         % (r.get("night") or 0, r.get("seat"), verdict))
        return "; ".join(parts)

    if rivals and not claimed:
        return fold("Counter-claim: I am the real seer. %s. "
                    "Seat %d is lying to draw your votes."
                    % (results_text() or "No results yet",
                       rivals[0]))
    if found:
        w = found[0].get("seat")
        if not claimed:
            return fold("I am the seer, and it is time you knew. %s. "
                        "Vote seat %d today, that is a confirmed werewolf. "
                        "Everyone on seat %d, no splitting: a tie exiles "
                        "nobody and the wolves get a free night."
                        % (results_text(), w, w))
        return fold("As I said, I am the seer and seat %d is a werewolf. "
                    "Vote seat %d and the village is nearly home."
                    % (w, w))
    if claimed and results:
        # Already out: keep the whole record in front of the village so
        # the clears vote as a bloc with me.
        target = _top_suspect(st, {r.get("seat") for r in clears})
        if target is not None:
            return fold("Your seer, again with my full record: %s. "
                        "Un-checked and un-cleared, my vote today is "
                        "seat %d." % (results_text(), target))
    pressured = (st.accusations_against(st.me) >= 2
                 or (st.day > 1 and sum(1 for (d, _s, t) in st.votes
                                         if t == st.me
                                         and d == st.day - 1) >= 2))
    if pressured and results and not claimed:
        # About to be exiled in the dark: a revealed seer's record is
        # worth more than a hidden one, even with no wolf found yet.
        return fold("Before this vote goes wrong: I am the seer. %s. "
                    "Exile me and you exile the village's only eyes; "
                    "the wolves are pushing this for a reason."
                    % results_text())
    if clears and st.day >= 3 and not claimed:
        clear_txt = ", ".join("seat %d" % r.get("seat") for r in clears)
        rest = [s for s in st.living()
                if s != st.me and s not in {r.get("seat") for r in clears}]
        return fold("I am the seer. %s. The werewolves are hiding among %s."
                    % (results_text(),
                       ", ".join("seat %d" % s for s in rest) or "the rest"))
    if clears:
        c = clears[-1].get("seat")
        if st.accusations_against(c) >= 1 or st.votes_against_today(c) >= 1:
            return fold("Hold on. I trust seat %d completely; they are not "
                        "a werewolf. Look elsewhere today." % c)
    target = _top_suspect(st, {r.get("seat") for r in clears})
    if target is not None and st.my_speeches_today() == 0:
        return fold("No claim from me yet. My strongest read: %s, because "
                    "they %s." % (st.label(target), _reason_for(st, target)))
    if target is not None:
        return fold("I will vote %s today. The numbers only work if we "
                    "stay together on one target." % st.label(target))
    return fold("Quiet day. I am watching the votes more than the words.")


def speech_village_side(st):
    claimant, named_wolves, named_clears = st.claim_analysis()
    if claimant is not None and st.me in (st.seer_claims.get(claimant) or {}).get("wolf", set()):
        return fold("Seat %d claims I am a werewolf. I am not, so that seer "
                    "claim is fake: seat %d is a werewolf hiding behind it. "
                    "Vote seat %d." % (claimant, claimant, claimant))
    status = st.claim_status()
    caught = [c for c in st.other_seer_claimants()
              if status.get(c) == "discredited"]
    if caught:
        c = caught[0]
        return fold("Seat %d's seer claim is proven false: a check they "
                    "announced does not match a revealed role, and a real "
                    "seer cannot be wrong. Seat %d is a werewolf in a "
                    "costume. Vote seat %d." % (c, c, c))
    if len(st.other_seer_claimants()) >= 2:
        a, b = st.other_seer_claimants()[:2]
        return fold("Two seer claims, seats %d and %d, and one of them is "
                    "lying. That is where today's vote belongs."
                    % (a, b))
    credible = st.credible_claimant()
    if credible is not None:
        rec = st.seer_claims.get(credible) or {"wolf": set(), "clear": set()}
        named = sorted(s for s in rec["wolf"] if st.alive(s))
        if named:
            w = named[0]
            return fold("One credible seer claim, from %s, and it names "
                        "%s as a werewolf. I back it: I will vote seat %d. "
                        "Everyone on seat %d: a split vote exiles nobody "
                        "and hands the wolves a free night."
                        % (st.label(credible), st.label(w), w, w))
    if claimant is not None and named_wolves:
        w = sorted(named_wolves)[0]
        return fold("Only one seer claim so far, from %s, and it names "
                    "%s as a werewolf. I back it: I will vote seat %d."
                    % (st.label(claimant), st.label(w), w))
    if st.votes_against_today(st.me) >= 2 or (
            st.day > 1 and sum(1 for (d, _s, t) in st.votes
                               if t == st.me and d == st.day - 1) >= 2):
        return fold(_defence(st))
    target = _top_suspect(st, named_clears)
    if target is None:
        return fold("Not much to go on yet. I am village and I am listening.")
    if st.my_speeches_today() == 0:
        return fold("My read today: %s, because they %s. I want their "
                    "answer before we vote."
                    % (st.label(target), _reason_for(st, target)))
    return fold("Sticking with my read: I will vote %s today."
                % st.label(target))


def _fake_seer_results(st, force_wolf=None):
    """Fabricated seer results for a werewolf, derived from public state.

    'Not a werewolf' claims name real non-werewolves (always safe), and the
    single 'werewolf' claim names the village player this wolf most wants
    exiled. If I already fake-claimed, reuse the same names so the story
    never changes. force_wolf pins the accused seat (a counter-claim names
    the rival claimant), unless an earlier claim of mine already fixed it.
    """
    mine = st.my_claimed_results()
    if mine["wolf"]:
        wolf_pick = sorted(mine["wolf"])[0]
        clears = sorted(mine["clear"])
        return wolf_pick, clears
    non_wolves = [s for s in st.living()
                  if s != st.me and s != st.partner]
    if not non_wolves:
        return None, []
    if force_wolf is not None and force_wolf in non_wolves:
        target = force_wolf
    else:
        target = _top_suspect(st, set())
    if target is None or target == st.partner or target not in non_wolves:
        target = non_wolves[0]
    dead_villagers = [s for (d, s, r) in st.dawns if r not in (None, "werewolf")]
    dead_villagers += [s for (d, s, r) in st.exiles if r == "villager"]
    clears = []
    for s in dead_villagers:
        if s not in clears:
            clears.append(s)
    if not clears:
        others = [s for s in non_wolves if s != target]
        if others:
            clears.append(others[0])
    return target, clears[:2]


def speech_wolf(st):
    claimant, named_wolves, _c = st.claim_analysis()
    claimed_fake = st.i_claimed_seer()
    partner = st.partner
    accused = (st.votes_against_today(st.me) >= 1
               or st.accusations_against(st.me) >= 2
               or (st.day > 1 and sum(1 for (d, _s, t) in st.votes
                                      if t == st.me and d == st.day - 1) >= 2))
    partner_named = (claimant is not None and partner is not None
                     and partner in named_wolves)
    i_named = claimant is not None and st.me in named_wolves

    if (not claimed_fake and st.day >= 2 and claimant is None
            and not st.other_seer_claimants() and partner is not None
            and st.alive(partner)):
        # No seer has shown themselves by day 2: take the coat first.
        # The village follows the first claim it hears, and a fabricated
        # record costs nothing until a real seer stands up.
        wolf_pick, clears = _fake_seer_results(st)
        if wolf_pick is not None:
            clear_txt = ""
            if clears:
                clear_txt = (" I checked seat %d on night 1: not a "
                             "werewolf." % clears[0])
            return fold("I am the seer.%s Last night I checked seat %d: "
                        "a werewolf. Vote seat %d, and protect me tonight."
                        % (clear_txt, wolf_pick, wolf_pick))
    if claimed_fake:
        wolf_pick, clears = _fake_seer_results(st)
        if wolf_pick is not None and st.alive(wolf_pick):
            return fold("I am the seer, as I said. Seat %d is a werewolf, "
                        "my checks do not lie. Vote seat %d."
                        % (wolf_pick, wolf_pick))
        return fold("I am the seer. My cleared players have not steered "
                    "us wrong; follow the votes I called for.")
    if i_named or (accused and st.day >= 2 and claimant is None):
        # Cornered: counter-claim seer with a fabricated record.
        wolf_pick, clears = _fake_seer_results(st)
        if wolf_pick is not None:
            clear_txt = ""
            if clears:
                clear_txt = (" I also checked seat %d: not a werewolf."
                             % clears[0])
            return fold("Enough. I am the real seer and I stayed quiet to "
                        "stay alive.%s Last night I checked seat %d: a "
                        "werewolf. Vote seat %d."
                        % (clear_txt, wolf_pick, wolf_pick))
    if partner_named:
        if st.accusations_against(partner) >= 2:
            # The partner is going down today whatever I say; standing
            # next to them only buries me too. Distance, cheaply.
            return fold("The claim names %s, and half the table already "
                        "leans that way. I do not love deciding on one "
                        "claim, but I will not split the village over it."
                        % st.label(partner))
        if st.day >= 3 and claimant is not None:
            # Late game, a soft defence changes nothing: counter-claim and
            # accuse the real seer. The village must pick a claimant, and
            # either mistake costs them the game.
            wolf_pick, _cl = _fake_seer_results(st, force_wolf=claimant)
            if wolf_pick is not None:
                return fold("Wait. I am the real seer; I stayed quiet "
                            "because a loud seer dies. I checked seat %d: "
                            "a werewolf. That claim against %s is the "
                            "wolves protecting seat %d. Vote seat %d."
                            % (wolf_pick, st.label(partner), wolf_pick,
                               wolf_pick))
        return fold("A claim is not proof. Before we exile %s on one "
                    "player's word, I want to hear everyone. No counter-claim "
                    "yet either way." % st.label(partner))
    if claimant is not None and named_wolves:
        w = sorted(named_wolves)[0]
        return fold("The seer claim names %s. I have no reason to doubt it "
                    "yet, so I will vote seat %d." % (st.label(w), w))
    if accused:
        return fold(_defence(st))
    target = _top_suspect(st, {partner} if partner else set())
    if target is None or target == partner:
        return fold("I am village. Still forming reads; the votes will "
                    "tell us more than speeches do.")
    if st.my_speeches_today() == 0:
        return fold("Something is off about %s: they %s. I lean toward "
                    "voting there." % (st.label(target), _reason_for(st, target)))
    return fold("I will vote %s today, for the reasons I gave."
                % st.label(target))


def decide_speak(st, max_chars):
    if st.role == "seer":
        text = speech_seer(st)
    elif st.role == "werewolf":
        text = speech_wolf(st)
    else:
        text = speech_village_side(st)
    text = fold(text)
    if not text:
        return None
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].strip()
    return text


# --------------------------------------------------------------------------
# Dispatch: one pending decision -> one /act body. Never raises.
# --------------------------------------------------------------------------

def fallback_act(game, decision):
    if decision == "speak":
        return {"game": game, "decision": "speak", "pass": True}
    return {"game": game, "decision": decision, "target": None}


def decide(pending):
    game = pending.get("game")
    decision = pending.get("decision")
    view = pending.get("view") or {}
    try:
        st = GameState(view)
        if decision == "speak":
            text = decide_speak(st, pending.get("max_chars") or 500)
            if text:
                return {"game": game, "decision": "speak", "text": text}
            return fallback_act(game, decision)
        options = pending.get("options") or []
        if decision == "kill":
            target = decide_kill(st, options)
        elif decision == "inspect":
            target = decide_inspect(st, options)
        elif decision == "protect":
            target = decide_protect(st, options)
        elif decision == "vote":
            target = decide_vote(st, options)
        else:
            return fallback_act(game, decision)
        if target is not None and target not in options:
            target = None
        return {"game": game, "decision": decision, "target": target}
    except Exception:
        return fallback_act(game, decision)


def main():
    if not _env_url():
        sys.stderr.write("MAKEFUN_ENV_URL is not set\n")
        return 1
    stalls = 0
    for _ in range(200000):
        status, obs = env_post("/observe", {})
        if status != 200 or not isinstance(obs, dict):
            stalls += 1
            if stalls > 60:
                sys.stderr.write("observe keeps failing (status %s)\n" % status)
                return 1
            time.sleep(2)
            continue
        stalls = 0
        if obs.get("done"):
            return 0
        pending = obs.get("pending") or []
        for p in pending:
            act = decide(p)
            status, _resp = env_post("/act", act)
            if status != 200:
                # Refused: answer with the safe fallback for that decision.
                fb = fallback_act(p.get("game"), p.get("decision"))
                if fb != act:
                    env_post("/act", fb)
        if not pending:
            time.sleep(0.25)
    sys.stderr.write("iteration cap reached\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())

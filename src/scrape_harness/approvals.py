"""Mechanical network approval: the operator's fingerprint IS the approval (operator decision 2026-11-08).
It lives in the CLI itself, so it holds whichever agent or session calls it:

  <cli> <networked command> ... --plan   -> canonical plan + plan_hash (sha256); no network
  <cli> approve <plan_hash> [--ttl 30m]  -> shows the plan summary (and a macOS notification), then signs
                                            the approval with the operator's Secure Enclave key through
                                            Secretive's SSH agent: macOS asks for Touch ID; no touch, no
                                            approval. Agents MAY invoke it; only a finger completes it.
  <cli> <networked command> ...          -> recomputes plan_hash; refuses (exit 10) unless a matching,
                                            unexpired, UNUSED, validly signed approval exists; uses it

Signatures are OpenSSH signatures (`ssh-keygen -Y sign/verify`, namespace "scrape-harness-approval").
A line counts only if BOTH hold (the operator accepted that whoever can write the verifier pin's folder
could swap it; a swap now needs two changes):
  - it verifies against the allowed_signers file named by <PREFIX>_APPROVAL_SIGNERS (kept outside any
    repo; unset or unreadable -> refuse), principal "scrape-harness-approver";
  - the signing key's fingerprint equals pipeline.toml [consumer].approval_key_fingerprint.

The ledger (STATE/approvals/ledger.jsonl, approval.v2, append-only, 0600) is HASH-CHAINED: each line
carries `prev`, the sha256 of the line before it ("0"*64 for the first). A deleted, edited or reordered
line breaks the chain, and a broken chain refuses every approval until the operator looks. The chain
cannot anchor its own LAST line, so an approval also counts as used when a run record (DATA/runs) names
it: replaying one by truncating the ledger needs the run record removed too. An `approved`
line's signature covers its `prev`, so it cannot be moved either. A `used` line needs no signature
(forging one can only burn an approval, never grant one). Older approval.v1 lines are never honoured.

Line kinds. Signed (they GRANT authority): `approved` (one exact plan, once), `standing` (bounds for
scheduled panel runs, up to 90 days) and `bounded` (ONE `<cli> refresh` chain: one signature over the
refresh's bounds — site, egress, proxy, variants, backends, rate, concurrency, the exact crawl-lists
argument sets (one per crawl-lists step, each crawled once per refresh run), the most list pages and ad requests, the allowed fetch reasons, the step names — for at most
24 h; its plan_hash is the sha256 of the bounds, which are also the saved plan). Unsigned (they can only
REDUCE authority, or record it being used): `used`, `refunded`, `revoked`, and `spent` (a bounded
approval's terminal line: its refresh completed, or stopped for good).

A bounded approval belongs to ONE refresh run id (its state file, STATE/refresh/<run_id>.json, names
it). Every networked step of that refresh consumes it with consume_bounded: a `used` line naming the
refresh run id, the step and the step's list pages / ad requests. The step is refused (NotApproved, exit
10) when the approval is unsigned, revoked, expired, spent, or was ever used by ANOTHER refresh run id;
it is stopped (BoundExceeded, exit 3) when it would leave the bounds, cumulative counts included. The
same run id may use it again when it resumes after a failure. As with single-use approvals, the run
records (DATA/runs) are a second, independent record of each use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import contracts, pipeline, store

EXIT_UNAPPROVED = 10
FMT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_TTL = "30m"
NAMESPACE = "scrape-harness-approval"
PRINCIPAL = "scrape-harness-approver"
GENESIS = "0" * 64
SECRETIVE_SOCKET = "~/Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh"


class NotApproved(Exception):  # noqa: N818 - a refusal carried to the CLI as exit 10
    pass


class BoundExceeded(Exception):  # noqa: N818 - a refresh step outside its signed bounds: exit 3, nothing spent
    pass


SIGNED_KINDS = ("approved", "standing", "bounded")  # the kinds that grant authority; each carries a sig


def canonical(plan: dict) -> tuple[str, str]:
    """(canonical JSON, sha256 hex). Sorted keys, no whitespace: the same plan always hashes the same."""
    text = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text, hashlib.sha256(text.encode()).hexdigest()


def _dir(name: str) -> Path:
    p = store.state_dir() / name
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def save_plan(plan: dict) -> str:
    text, h = canonical(plan)
    (_dir("plans") / f"{h}.json").write_text(text + "\n")
    return h


def load_plan(plan_hash: str) -> dict:
    p = _dir("plans") / f"{plan_hash}.json"
    if not p.is_file():
        raise SystemExit(f"no saved plan {plan_hash[:12]}...; run the command with --plan first")
    return json.loads(p.read_text())


# ── the ledger: append-only, hash-chained ────────────────────────────────────────────────────


def _ledger() -> Path:
    return _dir("approvals") / "ledger.jsonl"


def _line(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True)


def chain() -> list[dict]:
    """Every approval.v2 line, with its chain checked. Raises NotApproved on a broken chain."""
    p = _ledger()
    if not p.exists():
        return []
    out, prev = [], GENESIS
    for n, raw in enumerate(p.read_text().splitlines(), 1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            raise NotApproved(
                f"the approval ledger has an unreadable line {n}; nothing is honoured until fixed"
            ) from None
        if rec.get("schema") != "approval.v2":
            continue  # pre-v2 lines (keyring-signed or unsigned): kept, never honoured, outside the chain
        if contracts.errors("approval.v2", rec):
            raise NotApproved(
                f"the approval ledger's line {n} is not a valid approval.v2; nothing is honoured"
            )
        if rec["prev"] != prev:
            raise NotApproved(
                f"the approval ledger's chain is broken at line {n}; nothing is honoured until fixed"
            )
        prev = hashlib.sha256(raw.encode()).hexdigest()
        out.append(rec)
    return out


def _head() -> str:
    p = _ledger()
    lines = [x for x in (p.read_text().splitlines() if p.exists() else []) if '"approval.v2"' in x]
    return hashlib.sha256(lines[-1].encode()).hexdigest() if lines else GENESIS


def _append(rec: dict) -> None:
    contracts.validate("approval.v2", rec)
    p = _ledger()
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (_line(rec) + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(p, 0o600)


def _signed_body(rec: dict) -> bytes:
    """What the signature covers: the whole record except the signature itself, canonical JSON."""
    return canonical({k: v for k, v in rec.items() if k != "sig"})[0].encode()


# ── the verifier pin: allowed_signers (Drive) + the fingerprint in pipeline.toml ────────────────


def _ssh_keygen() -> str:
    exe = shutil.which("ssh-keygen")
    if not exe:
        raise NotApproved("ssh-keygen is not on PATH; cannot sign or verify approvals")
    return exe


def signers_file() -> Path:
    name = pipeline.knob_name("APPROVAL_SIGNERS")
    raw = os.environ.get(name)
    if not raw:
        raise NotApproved(f"{name} is not set: it must name the allowed_signers file (outside any repo)")
    p = Path(raw).expanduser()
    if not p.is_file() or not os.access(p, os.R_OK):
        raise NotApproved(f"{name} does not name a readable file; failing closed")
    return p


def signer_key() -> str:
    """The `<type> <base64>` public key of the one allowed signer (principal + namespace checked)."""
    for line in signers_file().read_text().splitlines():
        parts = line.split()  # <principal> namespaces="<ns>" <keytype> <base64> [comment]
        if len(parts) >= 4 and parts[0] == PRINCIPAL and parts[1] == f'namespaces="{NAMESPACE}"':
            return f"{parts[2]} {parts[3]}"
    raise NotApproved(f"the allowed_signers file has no {PRINCIPAL} line for namespace {NAMESPACE}")


def fingerprint(pubkey: str) -> str:
    with tempfile.TemporaryDirectory(dir=_dir("approvals")) as d:
        f = Path(d) / "key.pub"
        f.write_text(pubkey + "\n")
        r = subprocess.run(
            [_ssh_keygen(), "-l", "-E", "sha256", "-f", str(f)], capture_output=True, text=True
        )
    m = re.search(r"SHA256:[A-Za-z0-9+/]{43}", r.stdout)
    if r.returncode != 0 or not m:
        raise NotApproved("the allowed signer's key is not a valid public key")
    return m.group(0)


def pinned_signer() -> tuple[str, str]:
    """(public key, fingerprint) — only if the Drive file and the pipeline.toml pin agree."""
    key = signer_key()
    fp, pin = fingerprint(key), pipeline.consumer().approval_key_fingerprint
    if fp != pin:
        raise NotApproved(
            "the allowed_signers key does not match pipeline.toml [consumer].approval_key_fingerprint; "
            "refusing (a pin swap needs both files changed)"
        )
    return key, fp


def verify(rec: dict, key_fp: tuple[str, str] | None = None) -> bool:
    if rec.get("kind") not in SIGNED_KINDS or not rec.get("sig"):
        return False
    _, fp = key_fp or pinned_signer()
    if rec.get("key_fp") != fp:
        return False
    with tempfile.TemporaryDirectory(dir=_dir("approvals")) as d:
        sig = Path(d) / "msg.sig"
        sig.write_text(rec["sig"])
        r = subprocess.run(
            [
                _ssh_keygen(),
                "-Y",
                "verify",
                "-f",
                str(signers_file()),
                "-I",
                PRINCIPAL,
                "-n",
                NAMESPACE,
                "-s",
                str(sig),
            ],
            input=_signed_body(rec),
            capture_output=True,
        )
    return r.returncode == 0 and fp.encode() in r.stdout


# ── signing: through an SSH agent (Secretive: Touch ID) or, in tests, a key file ───────────────


class AgentSigner:
    """Sign with the pinned public key through an SSH agent. With Secretive's agent, macOS shows the
    Touch ID prompt; the private key never leaves the Secure Enclave and is never read by this process."""

    def sign(self, body: bytes, pubkey: str) -> str:
        sock = os.path.expanduser(pipeline.knob("APPROVAL_AGENT") or SECRETIVE_SOCKET)
        if not os.path.exists(sock):
            raise NotApproved(f"no SSH agent socket at {sock} (is Secretive running?); nothing approved")
        with tempfile.TemporaryDirectory(dir=_dir("approvals")) as d:
            pub, msg = Path(d) / "signer.pub", Path(d) / "approval"
            pub.write_text(pubkey + "\n")
            msg.write_bytes(body)
            r = subprocess.run(
                [_ssh_keygen(), "-Y", "sign", "-f", str(pub), "-n", NAMESPACE, str(msg)],
                env={**os.environ, "SSH_AUTH_SOCK": sock},
                capture_output=True,
                text=True,
                timeout=120,
            )
            sig = msg.with_suffix(".sig")
            if r.returncode != 0 or not sig.is_file():
                raise NotApproved(
                    "not signed (Touch ID declined, timed out or the key was absent); nothing approved"
                )
            return sig.read_text()


SIGNER = AgentSigner


def parse_ttl(ttl: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([smh])", ttl.strip())
    if not m or int(m.group(1)) <= 0:
        raise SystemExit("--ttl must look like 45s, 30m or 2h")
    n, unit = int(m.group(1)), m.group(2)
    t = timedelta(seconds=n) if unit == "s" else timedelta(minutes=n) if unit == "m" else timedelta(hours=n)
    if t > timedelta(hours=24):
        raise SystemExit("--ttl is capped at 24h")
    return t


def summary(plan: dict, plan_hash: str, ttl: str) -> str:
    what = ", ".join(f"{k} {plan[k]}" for k in ("site", "variants", "backends") if plan.get(k))
    return f"Approve {plan['command']} ({what}) for {ttl}? plan {plan_hash[:12]}"


def _notify(text: str) -> None:
    """A macOS notification with the plan summary, so the operator sees WHAT the Touch ID prompt approves."""
    if sys.platform != "darwin" or pipeline.knob("NO_NOTIFY") == "1" or not shutil.which("osascript"):
        return
    script = f"display notification {json.dumps(text)} with title {json.dumps(pipeline.consumer().cli + ' approve')}"
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def approve(plan_hash: str, ttl: str = DEFAULT_TTL, stdout=None) -> dict:
    stdout = stdout or sys.stdout
    plan = load_plan(plan_hash)
    chain()  # never extend a broken chain
    key, fp = pinned_signer()
    text = summary(plan, plan_hash, ttl)
    stdout.write(json.dumps(plan, indent=1, sort_keys=True) + f"\nplan_hash {plan_hash}\n{text}\n")
    stdout.write("Touch ID will ask; the fingerprint approves exactly this plan.\n")
    stdout.flush()
    _notify(text)
    now = datetime.now(UTC)
    rec = {
        "schema": "approval.v2",
        "kind": "approved",
        "approval_id": secrets.token_hex(8),
        "plan_hash": plan_hash,
        "at": now.strftime(FMT),
        "expires_at": (now + parse_ttl(ttl)).strftime(FMT),
        "run_id": None,
        "key_fp": fp,
        "prev": _head(),
    }
    rec["sig"] = SIGNER().sign(_signed_body(rec), key)
    if not verify(rec, (key, fp)):
        raise NotApproved("the new signature does not verify against the pinned signer; nothing approved")
    _append(rec)
    return rec


def _run_uses() -> dict[str, list[str]]:
    """approval_id -> the creation times of the run records that USED it (a second, independent record of
    use: the ledger cannot anchor its own last line). A run refused before any site request and refunded
    (approval_ref.refunded) is not a use. One scan for every caller."""
    out: dict[str, list[str]] = {}
    for p in store.runs_dir().glob("*.json"):
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        ref = doc.get("approval_ref") or {}
        if ref.get("approval_id") and not ref.get("refunded"):
            out.setdefault(ref["approval_id"], []).append(doc.get("created_at") or "")
    return out


def _uses_by_runs() -> dict[str, list[str]]:
    return _run_uses()


def _used_by_runs() -> set[str]:
    return set(_run_uses())


def _spent(ledger: list[dict]) -> set[str]:
    """Approvals the LEDGER says are spent: revoked, or more `used` lines than `refunded` ones."""
    net: dict[str, int] = {}
    for r in ledger:
        if r["kind"] == "used":
            net[r["approval_id"]] = net.get(r["approval_id"], 0) + 1
        elif r["kind"] == "refunded":
            net[r["approval_id"]] = net.get(r["approval_id"], 0) - 1
    return {a for a, n in net.items() if n > 0} | {r["approval_id"] for r in ledger if r["kind"] == "revoked"}


def refund(approval_id: str, run_id: str, note: str, now: datetime | None = None) -> dict:
    """Give back ONE use of an approval whose run was refused before any site request. Append-only and
    two-sided: this ledger line AND the run record's approval_ref.refunded are both needed before consume()
    sees the approval as live again, so editing one root alone cannot replay it."""
    now = now or datetime.now(UTC)
    ledger = chain()
    if approval_id not in _spent(ledger):
        raise NotApproved(f"{approval_id}: nothing to refund (not spent, or already refunded)")
    a = next(r for r in ledger if r["approval_id"] == approval_id and r["kind"] == "approved")
    rec = {
        "schema": "approval.v2",
        "kind": "refunded",
        "approval_id": approval_id,
        "plan_hash": a["plan_hash"],
        "at": now.strftime(FMT),
        "expires_at": a["expires_at"],
        "run_id": run_id,
        "key_fp": a["key_fp"],
        "note": note,
        "prev": _head(),
    }
    _append(rec)
    return rec


def approved_plans(command: str, now: datetime | None = None) -> list[dict]:
    """The saved plan docs of `command` that an unexpired `approved` line names, the newest approval first.
    Candidates only (has_live/consume still check the signature and use): a run whose plan carries a signed
    value it cannot recompute (fetch-ads' as_of) rebuilds each candidate and keeps the one that holds a live
    approval. No ledger, no candidates: nothing is created on disk."""
    now = now or datetime.now(UTC)
    if not (store.state_dir() / "approvals" / "ledger.jsonl").exists():
        return []
    out = []
    for r in reversed(chain()):
        if r["kind"] != "approved" or _ts(r["expires_at"]) <= now:
            continue
        p = store.state_dir() / "plans" / f"{r['plan_hash']}.json"
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if doc.get("command") == command:
            out.append(doc)
    return out


def has_live(plan_hash: str, now: datetime | None = None) -> bool:
    """Whether a matching, unexpired, unused, validly signed approval exists (nothing is spent)."""
    now = now or datetime.now(UTC)
    ledger = chain()
    used = _spent(ledger) | _used_by_runs()
    pin = pinned_signer()
    return any(
        r["kind"] == "approved"
        and r["plan_hash"] == plan_hash
        and r["approval_id"] not in used
        and datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00")) > now
        and verify(r, pin)
        for r in ledger
    )


def consume(plan_hash: str, run_id: str | None = None, now: datetime | None = None) -> dict:
    """Use (append-only) the matching, unexpired, unused, validly signed approval, or raise NotApproved."""
    now = now or datetime.now(UTC)
    ledger = chain()
    mine = [r for r in ledger if r["kind"] == "approved" and r["plan_hash"] == plan_hash]
    cli = pipeline.consumer().cli
    if not mine:
        raise NotApproved(
            f"this exact run plan ({plan_hash[:12]}...) is not approved. Run the command with --plan, then "
            f"`{cli} approve {plan_hash}` (Touch ID)."
        )
    pin = pinned_signer()
    # Used = a `used` line OR a run record that names the approval. The chain cannot anchor its own last
    # line, so a deleted trailing `used` line would otherwise replay an approval; the run records (DATA)
    # are a second, independent record, so a replay needs both roots edited.
    used = _spent(ledger) | _used_by_runs()
    signed = [r for r in mine if verify(r, pin)]
    live = [
        r
        for r in signed
        if r["approval_id"] not in used
        and datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00")) > now
    ]
    if not live:
        why = "not validly signed by the pinned key" if not signed else "expired or already used"
        raise NotApproved(
            f"this exact run plan ({plan_hash[:12]}...) is {why}. Re-plan, then `{cli} approve`."
        )
    a = live[0]
    _append(
        {
            "schema": "approval.v2",
            "kind": "used",
            "approval_id": a["approval_id"],
            "plan_hash": plan_hash,
            "at": now.strftime(FMT),
            "expires_at": a["expires_at"],
            "run_id": run_id,
            "key_fp": a["key_fp"],
            "prev": _head(),
        }
    )
    return {
        "approval_id": a["approval_id"],
        "plan_hash": plan_hash,
        "approved_at": a["at"],
        "expires_at": a["expires_at"],
    }


def audit() -> dict:
    """`<cli> approvals verify`: the chain, and every signed line's signature against the pin."""
    ledger = chain()
    pin = pinned_signer()
    approved = [r for r in ledger if r["kind"] in SIGNED_KINDS]
    bad = [r["approval_id"] for r in approved if not verify(r, pin)]
    return {
        "lines": len(ledger),
        "approved": len(approved),
        "used": sum(r["kind"] == "used" for r in ledger),
        "standing": sum(r["kind"] == "standing" for r in ledger),
        "bounded": sum(r["kind"] == "bounded" for r in ledger),
        "refunded": sum(r["kind"] == "refunded" for r in ledger),
        "spent": sum(r["kind"] == "spent" for r in ledger),
        "revoked": sum(r["kind"] == "revoked" for r in ledger),
        "chain": "intact",
        "bad_signatures": bad,
        "key_fp": pin[1],
    }


# ── standing approvals: ONE signature over BOUNDS, for scheduled runs (the panel) ────────────────

MAX_STANDING = timedelta(days=90)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _approve_bounds(bounds: dict, kind: str, text: str, cap: timedelta, too_long: str, stdout=None) -> dict:
    """Touch ID signs ONE bounds document of this kind (standing | bounded); plan_hash = sha256 of the bounds.
    The plain-words `text` is shown (terminal and macOS notification) BEFORE the Touch ID prompt."""
    stdout = stdout or sys.stdout
    chain()
    now = datetime.now(UTC)
    if _ts(bounds["expires_at"]) - max(now, _ts(bounds["not_before"])) > cap:
        raise NotApproved(too_long)
    if _ts(bounds["expires_at"]) <= now:
        raise NotApproved("these bounds have already expired")
    key, fp = pinned_signer()
    stdout.write(json.dumps(bounds, indent=1, sort_keys=True) + f"\n{text}\nTouch ID will ask.\n")
    stdout.flush()
    _notify(text)
    rec = {
        "schema": "approval.v2",
        "kind": kind,
        "approval_id": secrets.token_hex(8),
        "plan_hash": canonical(bounds)[1],
        "at": now.strftime(FMT),
        "expires_at": bounds["expires_at"],
        "run_id": None,
        "key_fp": fp,
        "prev": _head(),
        "bounds": bounds,
    }
    rec["sig"] = SIGNER().sign(_signed_body(rec), key)
    if not verify(rec, (key, fp)):
        raise NotApproved("the new signature does not verify against the pinned signer; nothing approved")
    _append(rec)
    return rec


def approve_standing(bounds: dict, stdout=None) -> dict:
    """Touch ID signs these bounds once. Every scheduled run must then stay inside them (consume_standing)."""
    text = (
        f"STANDING approval for {bounds['site']}: up to {bounds['max_runs']} runs, each at most "
        f"{bounds['max_ad_requests_per_run']} ad pages + {bounds['max_list_pages_per_run']} list pages at "
        f"{bounds['max_rate_per_minute']}/min, at least {bounds['min_interval_s'] // 3600} h apart, egress "
        f"{bounds['egress_country']}, {bounds['not_before']} to {bounds['expires_at']}, panel {bounds['panel_hash'][:12]}"
    )
    return _approve_bounds(
        bounds,
        "standing",
        text,
        MAX_STANDING,
        "a standing approval may last at most 90 days; re-sign it as a review point",
        stdout,
    )


def revoke(approval_id: str, note: str) -> dict:
    """Withdraw an approval. Unsigned on purpose: a revocation can only REDUCE authority."""
    if not note.strip():
        raise SystemExit("a revocation needs a --note saying why")
    ledger = chain()
    target = next((r for r in ledger if r["approval_id"] == approval_id and r["kind"] in SIGNED_KINDS), None)
    if target is None:
        raise SystemExit(f"{approval_id}: no such approval")
    rec = {
        "schema": "approval.v2",
        "kind": "revoked",
        "approval_id": approval_id,
        "plan_hash": target["plan_hash"],
        "at": datetime.now(UTC).strftime(FMT),
        "expires_at": target["expires_at"],
        "run_id": None,
        "key_fp": target["key_fp"],
        "prev": _head(),
        "note": note.strip(),
    }
    _append(rec)
    return rec


def standing_bounds(panel_hash: str, now: datetime | None = None) -> list[dict]:
    """The bounds of every validly signed, unrevoked standing approval for this panel whose window is open,
    newest first (nothing is spent). A scheduled run uses it to find WHICH proxy its signature names."""
    now = now or datetime.now(UTC)
    ledger = chain()
    revoked = {r["approval_id"] for r in ledger if r["kind"] == "revoked"}
    pin = pinned_signer()
    return [
        r["bounds"]
        for r in reversed(ledger)
        if r["kind"] == "standing"
        and r["bounds"]["panel_hash"] == panel_hash
        and r["approval_id"] not in revoked
        and _ts(r["bounds"]["not_before"]) <= now < _ts(r["bounds"]["expires_at"])
        and verify(r, pin)
    ]


def consume_standing(
    run: dict, run_id: str | None = None, now: datetime | None = None, dry: bool = False
) -> dict:
    """A scheduled run proves it stays inside a valid standing approval, or is refused (exit 10).

    run: {panel_hash, site, ad_requests, list_pages, variant, rate_per_minute, egress_country}. Uses are
    counted from the ledger's `used` lines AND the run records naming the approval."""
    now = now or datetime.now(UTC)
    ledger = chain()
    revoked = {r["approval_id"] for r in ledger if r["kind"] == "revoked"}
    pin = pinned_signer()
    cands = [
        r
        for r in ledger
        if r["kind"] == "standing"
        and r["bounds"]["panel_hash"] == run["panel_hash"]
        and r["bounds"]["site"] == run["site"]
    ]
    if not cands:
        raise NotApproved(
            f"no standing approval for this panel ({run['panel_hash'][:12]}...) on {run['site']}"
        )
    why = []
    for a in reversed(cands):  # the newest first
        b, aid = a["bounds"], a["approval_id"]
        uses = sorted(
            [r["at"] for r in ledger if r["kind"] == "used" and r["approval_id"] == aid]
            + _uses_by_runs().get(aid, [])
        )
        n_uses = max(
            sum(1 for r in ledger if r["kind"] == "used" and r["approval_id"] == aid),
            len(_uses_by_runs().get(aid, [])),
        )
        checks = [
            (verify(a, pin), "not validly signed by the pinned key"),
            (aid not in revoked, "revoked"),
            (_ts(b["not_before"]) <= now < _ts(b["expires_at"]), "outside its window"),
            (
                _ts(b["expires_at"]) - _ts(a["at"]) <= MAX_STANDING + timedelta(minutes=1),
                "longer than 90 days",
            ),
            (n_uses < b["max_runs"], f"all {b['max_runs']} runs used"),
            (
                not uses or (now - _ts(max(uses))).total_seconds() >= b["min_interval_s"],
                "too soon after the last run",
            ),
            (
                run["ad_requests"] <= b["max_ad_requests_per_run"],
                f"{run['ad_requests']} ad pages > {b['max_ad_requests_per_run']}",
            ),
            (
                run["list_pages"] <= b["max_list_pages_per_run"],
                f"{run['list_pages']} list pages > {b['max_list_pages_per_run']}",
            ),
            (run["variant"] in b["variants"], f"variant {run['variant']} not signed"),
            (
                run["rate_per_minute"] <= b["max_rate_per_minute"],
                f"rate {run['rate_per_minute']}/min > {b['max_rate_per_minute']}",
            ),
            (
                run["egress_country"] == b["egress_country"],
                f"egress {run['egress_country']} is not {b['egress_country']}",
            ),
            (
                run.get("proxy") == b.get("proxy"),  # both absent, or the same URL
                f"proxy {run.get('proxy') or 'none'} is not the signed {b.get('proxy') or 'none'}",
            ),
        ]
        failed = [msg for ok, msg in checks if not ok]
        if not failed and dry:  # a --dry-run: every bound checked, nothing spent
            return {
                "approval_id": aid,
                "plan_hash": a["plan_hash"],
                "standing": True,
                "run": n_uses + 1,
                "dry": True,
            }
        if not failed:
            _append(
                {
                    "schema": "approval.v2",
                    "kind": "used",
                    "approval_id": aid,
                    "plan_hash": a["plan_hash"],
                    "at": now.strftime(FMT),
                    "expires_at": a["expires_at"],
                    "run_id": run_id,
                    "key_fp": a["key_fp"],
                    "prev": _head(),
                }
            )
            return {
                "approval_id": aid,
                "plan_hash": a["plan_hash"],
                "approved_at": a["at"],
                "expires_at": a["expires_at"],
                "standing": True,
                "run": n_uses + 1,
            }
        why.append(f"{aid}: {'; '.join(failed)}")
    raise NotApproved("this scheduled run is outside every standing approval: " + " | ".join(why))


# ── bounded approvals: ONE signature over a refresh's BOUNDS, for one on-demand chain ───────────

MAX_BOUNDED = timedelta(hours=24)
BOUNDED_TIMES = ("not_before", "expires_at")
LIST_KEYS = ("subcategory", "region", "page_count", "stride", "start", "seed", "max_pages")


def signed_lists(b: dict) -> list[dict]:
    """The signed crawl-lists argument sets, one per crawl-lists step in step order. A bounds doc signed
    before crawl-lists could repeat holds ONE set as an object ({} = none); it reads as a list of one."""
    lists = b.get("lists")
    if isinstance(lists, dict):
        return [lists] if lists else []
    return list(lists or [])


def _list_words(x: dict) -> str:
    how = (
        "a census"
        if x.get("stride") == 1
        else f"every {x.get('stride')}th page, start {x.get('start')}, seed {x.get('seed')}"
    )
    pages = (
        f"of its {x['page_count']} pages, at most {x.get('max_pages')}"
        if x.get("page_count")
        else f"page 1, then at most {x.get('max_pages')} more pages"
    )
    return f"{x.get('subcategory')} in {x.get('region')} ({how}; {pages})"


def bounded_text(b: dict) -> str:
    """The plain words the operator reads before Touch ID signs a refresh's bounds: each crawl-lists set
    (subcategory and pages), the totals, and the robots.txt check each site step makes."""
    sets = signed_lists(b)
    lp = (
        "crawl-lists "
        + "; ".join(f"({i}) {_list_words(x)}" for i, x in enumerate(sets, 1))
        + (", each set crawled once" if len(sets) > 1 else "")
        + "; "
        if sets
        else ""
    )
    site_steps = sum(1 for s in b["steps"] if s in pipeline.REFRESH_SITE_STEPS)
    return (
        f"BOUNDED approval for ONE refresh of {b['site']} (single use): steps {', '.join(b['steps'])}; "
        f"{lp}at most {b['max_list_pages']} list pages + {b['max_ad_requests']} ad pages in all, "
        f"plus one robots.txt check per site step ({site_steps}); "
        f"ad fetch reasons {', '.join(b['fetch_reasons']) or 'none'}"
        + (f", ad policy {b['policy_digest'][:19]}" if b.get("policy_digest") else "")
        + f"; variants {', '.join(b['variants'])} "
        f"via {', '.join(b['backends'])}; at most {b['max_rate_per_minute']}/min, concurrency "
        f"{b['concurrency']}{', politeness OVERRIDE' if b['politeness_override'] else ''}; egress "
        f"{b['egress_country']}, proxy {b.get('proxy') or 'none'}; {b['not_before']} to {b['expires_at']}"
    )


def approve_bounded(plan_hash: str, stdout=None) -> dict:
    """`<cli> approve <hash>` for a saved refresh plan: Touch ID signs its bounds (TTL fixed by the plan)."""
    bounds = load_plan(plan_hash)
    if bounds.get("command") != "refresh" or bounds.get("kind") != "bounded":
        raise SystemExit(f"plan {plan_hash[:12]}... is not a bounded refresh plan")
    if canonical(bounds)[1] != plan_hash:
        raise NotApproved(f"the saved plan {plan_hash[:12]}... does not hash to its name; nothing approved")
    return _approve_bounds(
        bounds,
        "bounded",
        bounded_text(bounds),
        MAX_BOUNDED,
        "a bounded refresh approval may last at most 24 h",
        stdout,
    )


def bounds_core(b: dict, proxies: tuple[str, ...] = ()) -> dict:
    """What decides a refresh's sends, without its clock (not_before/expires_at change on every plan) and,
    when several proxies are configured, without the proxy (the plan picks one; any configured one matches)."""
    drop = (*BOUNDED_TIMES, *(("proxy",) if len(proxies) > 1 else ()))
    return {k: v for k, v in b.items() if k not in drop} | {"lists": signed_lists(b)}


def _bounded_lines(ledger: list[dict], aid: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in ledger:
        if r["approval_id"] == aid:
            out.setdefault(r["kind"], []).append(r)
    return out


def _bounded_run_refs(aid: str) -> list[dict]:
    """The run records (DATA/runs) naming this bounded approval, unrefunded: the second root of use."""
    out = []
    for p in store.runs_dir().glob("*.json"):
        try:
            ref = json.loads(p.read_text()).get("approval_ref") or {}
        except (OSError, ValueError):
            continue
        if ref.get("approval_id") == aid and ref.get("bounded") and not ref.get("refunded"):
            out.append(ref)
    return out


def bounded_usage(aid: str, refresh_run_id: str, ledger: list[dict] | None = None) -> dict:
    """{list_pages, ad_requests} this refresh run has consumed: `used` minus `refunded` lines, or the run
    records' sum when larger (a deleted trailing ledger line cannot lower the count)."""
    lines = _bounded_lines(ledger if ledger is not None else chain(), aid)

    def net(k: str) -> int:
        mine = lambda kind: sum(r.get(k, 0) for r in lines.get(kind, []) if r["run_id"] == refresh_run_id)  # noqa: E731
        return mine("used") - mine("refunded")

    refs = [r for r in _bounded_run_refs(aid) if r.get("refresh_run") == refresh_run_id]
    return {k: max(net(k), sum(r.get(k, 0) for r in refs)) for k in ("list_pages", "ad_requests")}


def consumed_sets(aid: str, refresh_run_id: str, ledger: list[dict]) -> dict[int, int]:
    """Signed crawl-lists set index -> how often this refresh run used it (`used` minus `refunded`, or the
    run records' count when larger)."""
    lines = _bounded_lines(ledger, aid)
    net: dict[int, int] = {}
    for kind, d in (("used", 1), ("refunded", -1)):
        for r in lines.get(kind, []):
            if r["run_id"] == refresh_run_id and isinstance(r.get("list_set"), int):
                net[r["list_set"]] = net.get(r["list_set"], 0) + d
    refs: dict[int, int] = {}
    for r in _bounded_run_refs(aid):
        if r.get("refresh_run") == refresh_run_id and isinstance(r.get("list_set"), int):
            refs[r["list_set"]] = refs.get(r["list_set"], 0) + 1
    return {i: max(net.get(i, 0), refs.get(i, 0)) for i in {*net, *refs}}


def bounded_status(aid: str, ledger: list[dict] | None = None, now: datetime | None = None) -> str | None:
    """Why this bounded approval grants nothing (None: it is live). Signature, revocation, spent, window."""
    now = now or datetime.now(UTC)
    ledger = ledger if ledger is not None else chain()
    lines = _bounded_lines(ledger, aid)
    if not lines.get("bounded"):
        return "no such bounded approval"
    a = lines["bounded"][0]
    b = a["bounds"]
    if not verify(a, pinned_signer()):
        return "not validly signed by the pinned key"
    if lines.get("revoked"):
        return "revoked"
    if lines.get("spent"):
        return f"spent ({lines['spent'][0].get('note')})"
    if _ts(b["expires_at"]) - _ts(a["at"]) > MAX_BOUNDED + timedelta(minutes=1):
        return "longer than 24 h"
    if not _ts(b["not_before"]) <= now:
        return "not yet valid"
    if now >= _ts(b["expires_at"]):
        return "expired"
    return None


def bounded_claims(aid: str, ledger: list[dict] | None = None) -> set[str]:
    """Every refresh run id that has used this bounded approval (ledger AND run records)."""
    lines = _bounded_lines(ledger if ledger is not None else chain(), aid)
    return {r["run_id"] for r in lines.get("used", []) if r["run_id"]} | {
        r["refresh_run"] for r in _bounded_run_refs(aid) if r.get("refresh_run")
    }


def bounded_live(core: dict, proxies: tuple[str, ...] = (), now: datetime | None = None) -> list[dict]:
    """Live, UNCLAIMED bounded approvals whose bounds (without their clock) equal `core`, newest first."""
    now = now or datetime.now(UTC)
    ledger = chain()
    return [
        r
        for r in reversed(ledger)
        if r["kind"] == "bounded"
        and bounds_core(r["bounds"], proxies) == core
        and (not r["bounds"].get("proxy") or len(proxies) <= 1 or r["bounds"]["proxy"] in proxies)
        and not bounded_claims(r["approval_id"], ledger)
        and bounded_status(r["approval_id"], ledger, now) is None
    ]


def bounded_record(aid: str, ledger: list[dict] | None = None) -> dict | None:
    lines = _bounded_lines(ledger if ledger is not None else chain(), aid)
    return (lines.get("bounded") or [None])[0]


def _refresh_approval(refresh_run_id: str, ledger: list[dict]) -> dict:
    from . import refresh

    st = refresh.load_state(refresh_run_id)
    a = bounded_record(st["approval_id"], ledger)
    if a is None or a["plan_hash"] != st["plan_hash"]:
        raise NotApproved(f"refresh run {refresh_run_id} names no bounded approval with its plan_hash")
    return a


def refresh_bounds(refresh_run_id: str) -> dict:
    """The signed bounds of a refresh run's approval (a step reads its proxy from them)."""
    return _refresh_approval(refresh_run_id, chain())["bounds"]


def consume_bounded(
    step_doc: dict, refresh_run_id: str, step: str, now: datetime | None = None, dry: bool = False
) -> dict:
    """A refresh step proves it stays inside its refresh run's bounded approval, then uses it.

    step_doc: the step's run_plan doc plus list_pages, ad_requests (what it may send) and fetch_reasons.
    Refuses (NotApproved, exit 10) an approval that is missing, unsigned, revoked, expired or spent, or
    that another refresh run id ever used; stops (BoundExceeded, exit 3) a step outside the bounds. Either
    way nothing is appended. On success: a `used` line naming the refresh run id, the step and its counts."""
    now = now or datetime.now(UTC)
    ledger = chain()
    a = _refresh_approval(refresh_run_id, ledger)
    aid, b = a["approval_id"], a["bounds"]
    cli = pipeline.consumer().cli
    if why := bounded_status(aid, ledger, now):
        raise NotApproved(
            f"refresh run {refresh_run_id}'s approval {aid} is {why}; plan a new `{cli} refresh`"
        )
    if others := sorted(bounded_claims(aid, ledger) - {refresh_run_id}):
        raise NotApproved(
            f"approval {aid} was already used by refresh run {', '.join(others)}: a bounded approval is "
            f"single use; plan a new `{cli} refresh`"
        )
    if step_doc.get("command") == "fetch-ads":  # the ad policy first: a mismatch is no approval at all
        signed, sent = b.get("policy_digest"), step_doc.get("policy_digest")
        if not signed or signed != sent:
            raise NotApproved(
                f"refresh step fetch-ads runs ad policy {sent or 'none'}, but approval {aid} signs "
                f"{signed or 'no ad policy'}: the selection rules changed since the approval; plan a new "
                f"`{cli} refresh`"
            )
    used = bounded_usage(aid, refresh_run_id, ledger)
    lp, ar = int(step_doc.get("list_pages") or 0), int(step_doc.get("ad_requests") or 0)
    reasons = sorted(set(step_doc.get("fetch_reasons") or []))
    list_set, list_checks = None, []
    if step_doc.get("command") == "crawl-lists":  # its args must be one signed set this run has not crawled
        args = {k: step_doc.get(k) for k in LIST_KEYS}
        sets, done = signed_lists(b), consumed_sets(aid, refresh_run_id, ledger)
        match = [i for i, x in enumerate(sets) if x == args]
        free = [i for i in match if done.get(i, 0) <= 0]
        list_set = free[0] if free else None
        list_checks = [
            (
                bool(match),
                f"crawl-lists arguments {json.dumps(args, sort_keys=True)} are not one of the signed "
                f"{json.dumps(sets, sort_keys=True)}",
            ),
            (
                not match or bool(free),
                f"crawl-lists set {match[0] + 1 if match else '?'} was already crawled in refresh run "
                f"{refresh_run_id}: each signed set is crawled once",
            ),
        ]
    checks = [
        (step in b["steps"], f"step {step} is not one of the signed steps {', '.join(b['steps'])}"),
        (step_doc.get("site") == b["site"], f"site {step_doc.get('site')} is not {b['site']}"),
        (
            step_doc.get("egress_country") == b["egress_country"],
            f"egress {step_doc.get('egress_country')} is not {b['egress_country']}",
        ),
        (
            step_doc.get("proxy") == b.get("proxy"),
            f"proxy {step_doc.get('proxy') or 'none'} is not the signed {b.get('proxy') or 'none'}",
        ),
        (
            set(step_doc.get("variants") or []) <= set(b["variants"]),
            f"variants {step_doc.get('variants')} are not all signed ({b['variants']})",
        ),
        (
            set(step_doc.get("backends") or []) <= set(b["backends"]),
            f"backends {step_doc.get('backends')} are not all signed ({b['backends']})",
        ),
        (
            (step_doc.get("max_tasks_per_minute") or 0) <= b["max_rate_per_minute"],
            f"rate {step_doc.get('max_tasks_per_minute')}/min > {b['max_rate_per_minute']}",
        ),
        (
            (step_doc.get("concurrency") or 0) <= b["concurrency"],
            f"concurrency {step_doc.get('concurrency')} > {b['concurrency']}",
        ),
        (
            not step_doc.get("politeness_override") or b["politeness_override"],
            "a politeness override that the bounds do not sign",
        ),
        *list_checks,
        (
            used["list_pages"] + lp <= b["max_list_pages"],
            f"{used['list_pages']} + {lp} list pages > {b['max_list_pages']}",
        ),
        (
            used["ad_requests"] + ar <= b["max_ad_requests"],
            f"{used['ad_requests']} + {ar} ad requests > {b['max_ad_requests']}",
        ),
        (
            set(reasons) <= set(b["fetch_reasons"]),
            f"fetch reasons {', '.join(sorted(set(reasons) - set(b['fetch_reasons'])))} not signed",
        ),
    ]
    if failed := [msg for ok, msg in checks if not ok]:
        raise BoundExceeded(f"refresh step {step} is outside its bounded approval {aid}: {'; '.join(failed)}")
    ref = {
        "approval_id": aid,
        "plan_hash": a["plan_hash"],
        "approved_at": a["at"],
        "expires_at": a["expires_at"],
        "bounded": True,
        "refresh_run": refresh_run_id,
        "step": step,
        "list_pages": lp,
        "ad_requests": ar,
        **({"list_set": list_set} if list_set is not None else {}),
    }
    if dry:
        return ref | {"dry": True}
    _append(_usage_line("used", a, refresh_run_id, step, lp, ar, now, list_set=list_set))
    return ref


def _usage_line(
    kind: str, a: dict, run_id: str, step: str, lp: int, ar: int, now: datetime, note=None, list_set=None
) -> dict:
    return {
        "schema": "approval.v2",
        "kind": kind,
        "approval_id": a["approval_id"],
        "plan_hash": a["plan_hash"],
        "at": now.strftime(FMT),
        "expires_at": a["expires_at"],
        "run_id": run_id,
        "key_fp": a["key_fp"],
        "step": step,
        "list_pages": lp,
        "ad_requests": ar,
        **({"list_set": list_set} if list_set is not None else {}),
        **({"note": note} if note else {}),
        "prev": _head(),
    }


def refund_bounded(ref: dict, note: str, now: datetime | None = None) -> dict:
    """Give back ONE step's use of a bounded approval when the step was refused before any site request
    (cooldown, egress, politeness): a `refunded` line with the same counts, and (by the caller) the run
    record's approval_ref.refunded, so the resume of the same refresh run id is not double counted."""
    now = now or datetime.now(UTC)
    ledger = chain()
    aid, rid, step = ref["approval_id"], ref["refresh_run"], ref["step"]
    lines = _bounded_lines(ledger, aid)

    def n(kind: str) -> int:
        return sum(
            1
            for r in lines.get(kind, [])
            if r["run_id"] == rid
            and r.get("step") == step
            and (r.get("list_pages"), r.get("ad_requests"), r.get("list_set"))
            == (ref["list_pages"], ref["ad_requests"], ref.get("list_set"))
        )

    if n("used") <= n("refunded"):
        raise NotApproved(f"{aid}: nothing to refund for {rid} step {step}")
    a = lines["bounded"][0]
    rec = _usage_line(
        "refunded", a, rid, step, ref["list_pages"], ref["ad_requests"], now, note, ref.get("list_set")
    )
    _append(rec)
    return rec


def mark_spent(approval_id: str, refresh_run_id: str, why: str, now: datetime | None = None) -> dict | None:
    """The bounded approval's terminal line (unsigned: it only reduces authority). None if already spent."""
    now = now or datetime.now(UTC)
    ledger = chain()
    lines = _bounded_lines(ledger, approval_id)
    if not lines.get("bounded"):
        raise SystemExit(f"{approval_id}: no such bounded approval")
    if lines.get("spent"):
        return None
    a = lines["bounded"][0]
    rec = {
        "schema": "approval.v2",
        "kind": "spent",
        "approval_id": approval_id,
        "plan_hash": a["plan_hash"],
        "at": now.strftime(FMT),
        "expires_at": a["expires_at"],
        "run_id": refresh_run_id,
        "key_fp": a["key_fp"],
        "note": why,
        "prev": _head(),
    }
    _append(rec)
    return rec

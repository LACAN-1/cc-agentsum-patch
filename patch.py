#!/usr/bin/env python3
"""cc-agentsum-patch: let a Claude Code session on a third-party provider use the
"short" background-agent progress summaries.

Unofficial. Not affiliated with Anthropic. Never modifies the installed binary:
it reads it, writes a patched COPY into its own directory, and ad-hoc signs the copy.

Subcommands:
  patch   (default) build the patched copy
  check   read a --debug-file log and say whether summaries ran in short or full mode
  status  list patched copies this tool has built
  remove  delete every patched copy this tool has built

Python 3.9+ standard library only (macOS ships 3.9).
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import struct
import subprocess
import sys

TOOL_VERSION = "0.1.0"

# Claude Code versions verified end to end (patched, then a real background agent
# produced a short-mode summary). Anything else needs --allow-untested.
VERIFIED = {"2.1.316"}

DEFAULT_OUT = os.path.expanduser("~/.local/share/cc-agentsum-patch")
DEFAULT_LINK = os.path.expanduser("~/.local/bin/claude")

# Bun standalone module graph (as found in Claude Code 2.1.316's Mach-O build).
TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32  # u64 byte_count, StringPointer modules, u32 entry, StringPointer argv, u32 flags
ENTRY_SIZE = 52    # 6 StringPointers (name, contents, sourcemap, bytecode, ?, ?) + 4 x u8
BYTECODE_FIELD = 3

FLAG_NAME = b'"tengu_groovy_ember"'

# Mode switch: `return{mode:X().parse(e).mode,source:n}}` right after the flag name.
MODE_RE = re.compile(rb"return\{mode:\w+\(\)\.parse\(\w+\)\.mode,source:(\w+)\}\}")
# Summary-writer model picker: returns undefined (= stay on full) for any
# third-party provider or whenever a small/haiku model is configured.
MODEL_RE = re.compile(
    rb"function ([\w$]+)\(\)\{if\(([\w$]+)\.ANTHROPIC_SMALL_FAST_MODEL!==void 0\|\|"
    rb"\2\.ANTHROPIC_DEFAULT_HAIKU_MODEL!==void 0\|\|[^{}]{0,200}?\)return;"
    rb"let ([\w$]+)=[\w$]+\(\);return [^{}]{0,200}?\?\3:void 0\}"
)

SHORT_LINE = b"[AgentSummary] Asking for summary from"
FULL_LINE = b"[AgentSummary] Forking for summary"


class PatchError(Exception):
    pass


def say(msg):
    print(msg, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def same_length(old, new):
    """Pad `new` with spaces before its last byte so it replaces `old` in place."""
    if len(new) > len(old):
        raise PatchError("replacement longer than original")
    return new[:-1] + b" " * (len(old) - len(new)) + new[-1:]


def claude_version(binary):
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PatchError("could not run %s --version: %s" % (binary, e))
    m = re.match(r"\s*(\d+\.\d+\.\d+)", out)
    if not m:
        raise PatchError("unexpected --version output: %r" % out[:80])
    return m.group(1)


def parse_graph(d):
    t = d.rfind(TRAILER)
    if t < 0:
        raise PatchError("no Bun module graph found: not a Bun-compiled Claude Code binary")
    o = t - OFFSETS_SIZE
    byte_count = struct.unpack_from("<Q", d, o)[0]
    mods_off, mods_len = struct.unpack_from("<II", d, o + 8)
    base = o - byte_count
    if base < 0 or mods_len == 0 or mods_len % ENTRY_SIZE:
        raise PatchError("module graph layout not recognised (Bun format changed?)")
    entries = []
    for k in range(mods_len // ENTRY_SIZE):
        e = base + mods_off + k * ENTRY_SIZE
        ptrs = [struct.unpack_from("<II", d, e + 8 * j) for j in range(6)]
        name = d[base + ptrs[0][0]: base + ptrs[0][0] + ptrs[0][1]]
        if not name.startswith(b"/$bunfs/"):
            raise PatchError("module graph entry %d has no /$bunfs/ name (Bun format changed?)" % k)
        entries.append({
            "index": k,
            "entry_off": e,
            "name": name.decode("utf-8", "replace"),
            "start": base + ptrs[1][0],
            "end": base + ptrs[1][0] + ptrs[1][1],
            "bytecode": ptrs[BYTECODE_FIELD],
        })
    return entries


def module_at(entries, pos):
    hits = [e for e in entries if e["start"] <= pos < e["end"]]
    if len(hits) != 1:
        raise PatchError("offset %d is not inside exactly one module" % pos)
    return hits[0]


def only_match(regex, d, what):
    ms = list(regex.finditer(d))
    if len(ms) != 1:
        raise PatchError("%s: expected exactly 1 match, found %d. This Claude Code version "
                         "is laid out differently; refusing to guess." % (what, len(ms)))
    return ms[0]


def plan(d):
    entries = parse_graph(d)

    mode = only_match(MODE_RE, d, "mode switch")
    flag_at = d.rfind(FLAG_NAME, max(0, mode.start() - 400), mode.start())
    if flag_at < 0:
        raise PatchError("mode switch is not next to %s; refusing" % FLAG_NAME.decode())
    mode_new = same_length(mode.group(0), b'return{mode:"short",source:' + mode.group(1) + b"}}")

    model = only_match(MODEL_RE, d, "summary-writer model picker")
    fn, env = model.group(1), model.group(2)
    model_new = same_length(model.group(0), b"function " + fn + b"(){return " + env + b".ANTHROPIC_DEFAULT_HAIKU_MODEL}")

    mode_mod = module_at(entries, mode.start())
    model_mod = module_at(entries, model.start())
    # Cross-check: the module with the mode switch must call the picker we found.
    if (fn + b"()") not in d[mode_mod["start"]:mode_mod["end"]]:
        raise PatchError("the summariser module does not call %s(); refusing" % fn.decode())
    for m in {mode_mod["index"]: mode_mod, model_mod["index"]: model_mod}.values():
        if not d[m["start"]:m["start"] + 7] == b"// @bun":
            raise PatchError("module %s has an unexpected header" % m["name"])

    return {
        "edits": [(mode.start(), mode.group(0), mode_new), (model.start(), model.group(0), model_new)],
        "modules": sorted({mode_mod["index"]: mode_mod, model_mod["index"]: model_mod}.values(),
                          key=lambda m: m["index"]),
        "picker": fn.decode(),
    }


def apply(d, p):
    b = bytearray(d)
    for pos, old, new in p["edits"]:
        assert b[pos:pos + len(old)] == old and len(new) == len(old)
        b[pos:pos + len(old)] = new
    # The modules ship precompiled bytecode, which ignores edits to their source
    # text. Clearing the bytecode pointer makes Bun compile them from source.
    for m in p["modules"]:
        struct.pack_into("<II", b, m["entry_off"] + 8 * BYTECODE_FIELD, 0, 0)
    return bytes(b)


def cmd_patch(a):
    if platform.system() != "Darwin":
        raise PatchError("this release supports macOS only")
    src = os.path.realpath(a.src or DEFAULT_LINK)
    if not os.path.isfile(src):
        raise PatchError("Claude Code binary not found: %s (pass --src)" % src)
    ver = claude_version(src)
    say("source   %s\nversion  %s" % (src, ver))
    if ver not in VERIFIED and not a.allow_untested:
        raise PatchError("version %s is not verified (verified: %s). Re-run with --allow-untested "
                         "to try anyway; it still refuses if the code does not match exactly."
                         % (ver, ", ".join(sorted(VERIFIED))))
    before = sha256(src)
    with open(src, "rb") as f:
        d = f.read()
    p = plan(d)
    patched = apply(d, p)

    os.makedirs(a.out, exist_ok=True)
    out = os.path.join(a.out, "claude-%s-agentsum" % ver)
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(patched)
    os.chmod(tmp, 0o755)
    r = subprocess.run(["codesign", "-f", "-s", "-", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        os.remove(tmp)
        raise PatchError("codesign failed: %s" % r.stderr.strip())
    if claude_version(tmp) != ver:
        os.remove(tmp)
        raise PatchError("patched copy did not start cleanly; nothing installed")
    os.replace(tmp, out)

    if sha256(src) != before:
        raise PatchError("BUG: source binary changed during patching")

    manifest = {
        "tool_version": TOOL_VERSION, "claude_version": ver, "source": src,
        "source_sha256": before, "output": out, "output_sha256": sha256(out),
        "picker_function": p["picker"], "modules": [m["name"] for m in p["modules"]],
        "verified_version": ver in VERIFIED,
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(out + ".json", "w") as f:
        json.dump(manifest, f, indent=2)

    say("patched  %s" % out)
    say("source binary untouched (sha256 unchanged)")
    if ver not in VERIFIED:
        say("WARNING: %s is untested. Run `patch.py check` on a debug log before relying on it." % ver)
    say("\nNext: start your third-party-provider session with this binary instead of `claude`,")
    say("with ANTHROPIC_DEFAULT_HAIKU_MODEL set to the model that should write the labels.")
    if not os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        say("(ANTHROPIC_DEFAULT_HAIKU_MODEL is not set in this shell; without it the copy falls back to full mode.)")


def cmd_check(a):
    with open(a.log, "rb") as f:
        d = f.read()
    short, full = d.count(SHORT_LINE), d.count(FULL_LINE)
    say("short-mode summaries: %d\nfull-mode summaries:  %d" % (short, full))
    if short and not full:
        say("RESULT: working (short mode)")
    elif short and full:
        say("RESULT: mixed. Short mode ran, then fell back to full. A short-mode call probably failed;"
            " search the log for API errors near the first full-mode line.")
        return 1
    elif full:
        say("RESULT: not working (full mode only). Is ANTHROPIC_DEFAULT_HAIKU_MODEL set, and was the log"
            " produced by the patched copy?")
        return 1
    else:
        say("RESULT: no summaries in this log. Summaries only run for background subagents in an"
            " interactive session, after roughly a minute of work.")
        return 1
    return 0


def builds(out):
    if not os.path.isdir(out):
        return []
    return sorted(os.path.join(out, n) for n in os.listdir(out)
                  if re.fullmatch(r"claude-\d+\.\d+\.\d+-agentsum(\.json)?", n))


def cmd_status(a):
    files = [f for f in builds(a.out) if f.endswith(".json")]
    if not files:
        say("no patched copies in %s" % a.out)
    for f in files:
        with open(f) as fh:
            m = json.load(fh)
        ok = os.path.isfile(m["output"]) and sha256(m["output"]) == m["output_sha256"]
        say("%s  (Claude Code %s, built %s, %s)" % (m["output"], m["claude_version"], m["built_at"],
                                                    "intact" if ok else "MISSING OR CHANGED"))
    return 0


def cmd_remove(a):
    files = builds(a.out)
    for f in files:
        os.remove(f)
        say("removed %s" % f)
    if not files:
        say("nothing to remove in %s" % a.out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = ap.add_subparsers(dest="cmd")
    pp = sub.add_parser("patch", help="build the patched copy (default)")
    for p in (ap, pp):
        p.add_argument("--src", help="Claude Code binary to read (default: resolve ~/.local/bin/claude)")
        p.add_argument("--out", default=DEFAULT_OUT, help="where to write the copy (default: %(default)s)")
        p.add_argument("--allow-untested", action="store_true", help="try a version not in the verified list")
    pc = sub.add_parser("check", help="read a --debug-file log and report short/full mode")
    pc.add_argument("log")
    for name in ("status", "remove"):
        p = sub.add_parser(name)
        p.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check":
            return cmd_check(a)
        if a.cmd == "status":
            return cmd_status(a)
        if a.cmd == "remove":
            return cmd_remove(a)
        cmd_patch(a)
        return 0
    except PatchError as e:
        say("ERROR: %s" % e)
        say("Nothing was installed. The Claude Code binary was not modified.")
        return 2


if __name__ == "__main__":
    sys.exit(main())

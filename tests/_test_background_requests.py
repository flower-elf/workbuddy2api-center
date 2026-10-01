"""The background-request filter must not swallow a request the user asked for.

The filter matches a keyword against request_kind / turn_trigger /
thread_source, and the user's own "compact the context" request carries
request_kind=compaction - the same word as the background keyword. Once the
filter was switched on, pressing that button returned the refusal instead of a
summary. The two requests are told apart by the thread they run on: the
compaction the client starts by itself names the job that started it
(thread_source=memory_consolidation), while the operator's compaction runs on
the user's thread.

thread_source=user is not a general "the user asked for this" marker - an
auto_review the client fires on the user's thread carries it too, which is why
the exemption is scoped to request_kind=compaction instead of to the thread.

These cases pin that distinction, both shapes the metadata arrives in, and the
background jobs the filter exists for. No network access required.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-background-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_proxy as proxy

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def nested(meta):
    """The shape Codex actually sends: a JSON string under its own key."""
    return {"client_metadata": {"x-codex-turn-metadata": json.dumps(meta)}}


def flat(meta):
    return {"client_metadata": dict(meta)}


print("[1] the user's own compaction passes, in both metadata shapes")

check("nested compaction (request_kind + thread_source)",
      proxy.background_request_reason(
          nested({"request_kind": "compaction", "thread_source": "user"})) == "")
check("flat compaction (request_kind + thread_source)",
      proxy.background_request_reason(
          flat({"request_kind": "compaction", "thread_source": "user"})) == "")
check("compaction with no thread_source at all",
      proxy.background_request_reason(nested({"request_kind": "compaction"})) == "")
check("is_compaction_request agrees",
      proxy.is_compaction_request(nested({"request_kind": "compaction", "thread_source": "user"})))
check("a plain user turn is not a compaction",
      not proxy.is_compaction_request(nested({"request_kind": "chat", "thread_source": "user"})))

print()
print("[2] the background jobs the filter exists for are still refused")

background = [
    ("memory consolidation",
     {"request_kind": "memory", "thread_source": "memory_consolidation",
      "turn_trigger": "memory_consolidation"}),
    ("compaction started by the client",
     {"request_kind": "compaction", "thread_source": "memory_consolidation"}),
    ("title generation on the user's thread",
     {"request_kind": "title", "thread_source": "user"}),
    ("ambient suggestion",
     {"request_kind": "ambient_suggestion", "turn_trigger": "ambient_suggestions"}),
    ("auto review on the user's thread",
     {"request_kind": "auto_review", "thread_source": "user"}),
]
for label, meta in background:
    reason = proxy.background_request_reason(nested(meta))
    check("%s is refused" % label, bool(reason), (label, meta, reason))
    check("%s is not read as a user compaction" % label,
          not proxy.is_compaction_request(nested(meta)), (label, meta))

print()
print("[3] an unknown thread source is treated as the user's own request")

# Only the sources the client uses for jobs it starts are listed, so a source
# nobody has seen yet falls through to "the user asked for it". That is the
# deliberate direction: wrongly refusing here removes a button the user can
# see, while wrongly allowing only costs the credits the filter would save.
check("compaction with an unrecognised source",
      proxy.background_request_reason(
          nested({"request_kind": "compaction", "thread_source": "compaction"})) == "")
check("compaction with an unrecognised turn_trigger",
      proxy.background_request_reason(
          nested({"request_kind": "compaction", "turn_trigger": "user_initiated"})) == "")

print()
print("[4] requests without metadata are left alone")

check("no client_metadata", proxy.background_request_reason({"model": "m"}) == "")
check("empty client_metadata", proxy.background_request_reason({"client_metadata": {}}) == "")
check("client_metadata is not an object",
      proxy.background_request_reason({"client_metadata": "nope"}) == "")
check("a payload that is not an object", proxy.background_request_reason(None) == "")
check("unrelated metadata keys only",
      proxy.background_request_reason(flat({"thread_id": "t-1"})) == "")

print()
print("[5] the reason names the fields it matched on, for the log")

reason = proxy.background_request_reason(
    nested({"request_kind": "memory", "thread_source": "memory_consolidation"}))
check("the reason lists the field names", "request_kind" in reason, reason)
check("the reason lists the values", "memory_consolidation" in reason, reason)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
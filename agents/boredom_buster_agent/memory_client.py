"""
memory_client.py -- AgentCore Memory read/write helper for Boredom
Buster. Wraps bedrock_agentcore.memory.MemoryClient (the AgentCore
Python SDK's memory client -- confirmed against AWS's own "Amazon
Bedrock AgentCore SDK" memory guide, docs.aws.amazon.com/bedrock-
agentcore/latest/devguide/agentcore-sdk-memory.html, not assumed) with
the two operations this agent's tools actually need:
  - recording a conversation turn (so AgentCore Memory's SEMANTIC
    strategy can extract per-user preferences from it over time)
  - checking whether a specific title has prior recorded feedback

WHY A DIRECT CLIENT, NOT THE STRANDS SESSION-MANAGER INTEGRATION: AWS's
docs also show a `AgentCoreMemorySessionManager` that automatically
records every agent turn transparently (strands-sdk-memory.html). That's
a good fit for a plain conversational agent, but Boredom Buster's memory
needs are more specific than "remember the whole conversation" --
`check_history_tool` needs to query a per-title rating, and
`record_feedback_tool` needs to write a like/dislike as a distinct,
structured fact. Calling MemoryClient directly, at the exact points
those tools run, keeps memory reads/writes semantically meaningful
instead of being an opaque per-turn side effect of the agent loop.

MEMORY_ID/region come from environment variables set on the deployed
AgentCore agent -- see terraform/agentcore_memory.tf's
boredom_buster_memory_id output and this directory's README.md for how
that value gets wired in via `agentcore deploy`'s environment
configuration (not Terraform directly, since the agent itself isn't a
Terraform-managed resource).
"""

import logging
import os
import re

from bedrock_agentcore.memory import MemoryClient

logger = logging.getLogger("boredom_buster_agent.memory")

# Characters AgentCore Memory permits in an actorId / sessionId /
# namespace. Everything else is replaced with "-" by _memory_safe_id().
#
# WHY THIS EXISTS: Alexa's own identifiers are DOT-separated
# (session.user.userId is "amzn1.ask.account.XXXX", session.sessionId is
# "amzn1.echo-api.session.<uuid>") and AgentCore Memory rejects dots
# outright. Its API enforces:
#     actorId    [a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*...
#     sessionId  [a-zA-Z0-9][a-zA-Z0-9-_]*
#     namespace  [a-zA-Z0-9/*][a-zA-Z0-9-_/*]*(?::[a-zA-Z0-9-_/*]+)*...
# (the exact patterns quoted back in its own ValidationException
# messages, verified against the live API: create_event with a raw
# Alexa userId fails with "Value at 'actorId' failed to satisfy
# constraint", and retrieve_memories with "/preferences/<raw userId>/"
# fails the equivalent namespace check).
#
# Without sanitizing, every memory write and read fails validation:
# record_feedback catches the ValidationException and only logs it (by
# design -- a memory failure shouldn't block a recommendation), and the
# SDK's own retrieve_memories() swallows the error and returns an empty
# list, so check_history_tool would always report "no history for this
# title" regardless of what the user had actually rated.
#
# Sanitizing here (rather than at the Alexa Lambda's boundary) keeps the
# rule next to the API that imposes it, and means the raw Alexa IDs stay
# intact everywhere else they're used (call logs, allowlist, rate
# limiting, AgentCore Runtime's own runtimeSessionId -- none of which
# have this restriction).
_MEMORY_ID_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


def _memory_safe_id(raw_id: str) -> str:
    """
    Converts an Alexa identifier into one AgentCore Memory accepts.

    Deterministic and reversible-enough to be safe: the substitution
    (any disallowed character -> "-") cannot collide two distinct Alexa
    IDs in practice, because Alexa's userId/sessionId values are built
    from dot-separated segments of uppercase-alphanumerics and hex
    (sessionIds add hyphens inside their UUID), so no two real IDs
    differ only by a character that maps onto "-". Determinism is the
    property that actually matters: the same user must resolve to the
    same actor id on every future invocation, or their preferences would
    scatter across namespaces and never be found again.

    A leading non-alphanumeric character is also stripped, since all
    three AgentCore patterns require the first character to be
    alphanumeric.
    """
    sanitized = _MEMORY_ID_DISALLOWED.sub("-", raw_id or "")
    return sanitized.lstrip("-_") or "unknown"


class BoredomBusterMemory:
    """
    One instance per agent invocation (see agent.py's _build_agent),
    scoped to a specific actor_id (Alexa userId) + session_id (Alexa
    sessionId) pair -- see solution-design.md Section 12.3 for why these
    two IDs are kept distinct: actor_id is what makes long-term
    preferences persist per-user across sessions; session_id scopes the
    short-term SUMMARIZATION strategy to one conversation.
    """

    def __init__(self, actor_id: str, session_id: str):
        # Raw Alexa values kept for logging/traceability, since they're
        # what appear in the Lambda's own logs and DynamoDB call log --
        # but every AgentCore Memory API call uses the sanitized forms
        # below, because AgentCore rejects the dots Alexa's IDs contain.
        # See _memory_safe_id for the full reasoning and the bug this
        # closes.
        self.raw_actor_id = actor_id
        self.raw_session_id = session_id
        self.actor_id = _memory_safe_id(actor_id)
        self.session_id = _memory_safe_id(session_id)
        self.memory_id = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID", "")
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        # Client construction is cheap (no network call until a method
        # is actually invoked) -- safe to do unconditionally even if
        # memory_id ends up being unset, which _guard() below handles.
        self._client = MemoryClient(region_name=region)

    def _guard(self) -> bool:
        """Returns True if memory operations can proceed. Missing
        MEMORY_ID is treated as a soft failure (log + skip), not a hard
        error -- a misconfigured memory ID should degrade to
        "no memory this call" rather than crashing recommendations
        entirely, since the recommendation flow itself doesn't strictly
        require memory to function once, just to improve over time."""
        if not self.memory_id:
            logger.warning("BEDROCK_AGENTCORE_MEMORY_ID not set -- skipping memory operation")
            return False
        return True

    def record_feedback(self, title: str, liked: bool) -> None:
        """
        Records a like/dislike as a conversational event tagged with
        the title, so AgentCore Memory's SEMANTIC strategy (terraform/
        agentcore_memory.tf's boredom_buster_preferences strategy) can
        extract it into a durable per-user preference fact. Event
        write failures are logged, not raised -- see the module
        docstring's reasoning on graceful degradation.
        """
        if not self._guard():
            return

        sentiment = "liked" if liked else "disliked"
        try:
            # create_event's `messages` param is a list of (text, role)
            # tuples -- confirmed against the SDK's own create_event
            # docstring/signature, not assumed. This is the short-term
            # memory write; AgentCore Memory's SEMANTIC strategy
            # (terraform/agentcore_memory.tf) asynchronously extracts a
            # durable per-user preference fact from it afterward.
            self._client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[
                    (f"I {sentiment} '{title}'.", "USER"),
                    (f"Got it, I've noted that you {sentiment} '{title}'.", "ASSISTANT"),
                ],
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record feedback for title=%s liked=%s", title, liked)

    def get_title_rating(self, title: str) -> str | None:
        """
        Searches this user's long-term preference memory for a prior
        rating of `title`. Returns "liked", "disliked", or None if no
        relevant memory is found (including if memory is unavailable
        or the search itself fails -- see the module docstring's
        graceful-degradation reasoning; a memory-lookup failure should
        never block a recommendation from being made).
        """
        if not self._guard():
            return None

        try:
            # MemoryClient.retrieve_memories() is the high-level SDK
            # method for a semantic search over long-term memory
            # records -- confirmed against its actual signature
            # (memory_id, namespace, query, top_k) in the SDK source,
            # not assumed. It already returns a plain list of memory
            # record dicts (not a wrapped response needing a
            # "memoryRecordSummaries" key), and returns an empty list
            # rather than raising on most failure modes internally --
            # this method's own try/except is still kept as a defensive
            # backstop against anything that does raise.
            summaries = self._client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=f"/preferences/{self.actor_id}/",
                query=f"Did the user like or dislike '{title}'?",
                top_k=3,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to retrieve memory records for title=%s", title)
            return None

        for record in summaries:
            # A memory record's text content is nested under
            # content.text, mirroring the same {"content": {"text":
            # ...}} shape create_event's conversational payload uses --
            # verified against AWS's own retrieve-memory-records API
            # response documentation. Falls back to str(record) if that
            # shape isn't present, so a future API field-naming change
            # degrades to "no match found" rather than raising.
            content_text = record.get("content", {})
            if isinstance(content_text, dict):
                content_text = content_text.get("text", "")
            text = str(content_text or record).lower()

            if title.lower() not in text:
                continue
            if "disliked" in text or "dislike" in text:
                return "disliked"
            if "liked" in text or "like" in text:
                return "liked"
        return None

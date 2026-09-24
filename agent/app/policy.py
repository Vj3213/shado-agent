"""Loop-cap policy: hard-stops bot-to-bot conversations.

The counter lives in storage and is enforced atomically in SQL; this module
adds the human-reset rule (any non-peer message resets the counter) and the
read-check before generating a reply. Two layers of defense, not prompting.
"""

from __future__ import annotations

from storage.interface import MealAgentRepository


class LoopCapPolicy:
    def __init__(self, repo: MealAgentRepository, peer_bot_sender: str | None, max_turns: int):
        self._repo = repo
        self._peer = peer_bot_sender  # None = peer bot not yet discovered; logic dormant
        self._cap = max_turns

    def is_peer_bot(self, sender: str) -> bool:
        return self._peer is not None and sender == self._peer

    def note_human_activity(self, group_id: str) -> None:
        """A real human posted — the bot-to-bot counter resets."""
        if self._peer is None:
            return
        self._repo.reset_loop_state(group_id, self._peer)

    def turns_used(self, group_id: str) -> int:
        if self._peer is None:
            return 0
        state = self._repo.get_loop_state(group_id, self._peer)
        return state.consecutive_turns if state else 0

    def cap_reached(self, group_id: str) -> bool:
        return self.turns_used(group_id) >= self._cap

    def count_reply_to_peer(self, group_id: str) -> bool:
        """Atomically count a reply to the peer bot.

        Returns True if allowed (counter incremented), False if the cap
        blocked it — in which case the caller must NOT send the reply.
        """
        if self._peer is None:
            return True
        return self._repo.try_increment_bot_turns(group_id, self._peer, self._cap) is not None

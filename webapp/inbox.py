"""One durable input inbox, owned by exactly one conversation worker."""
from uuid import uuid4

from backend.conversation.turn_inbox import TurnStopped


class WebInbox:
    def __init__(self, store, run_id, initial_input):
        self.store, self.run_id = store, run_id
        self.batch_id = uuid4()
        self.initial_message_id = str(initial_input['input_id'])
        self.sealed = False
        self.store.claim_input(run_id, initial_input['input_id'], self.batch_id)

    def poll(self, *, seal=False):
        rows, stopped = self.store.poll_inputs(self.run_id, self.batch_id, seal=seal)
        if stopped:
            raise TurnStopped('用户请求停止后续处理')
        if seal and not rows:
            self.sealed = True
        return rows

    def ensure_sealed(self):
        # Adapters which do not implement graph checkpoints must use follow-up
        # turns, rather than claim that accepted input affected their answer.
        if not self.sealed:
            self.store.close_input(self.run_id)
            self.sealed = True

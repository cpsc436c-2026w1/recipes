"""A stand-in for the model, shaped like the OpenAI client: no network, no token, no cost.

Its first answer is a tool call to count_updates over the window; its second is a fixed reply that repeats the
number from the tool result. Tests and `--llm stub` use it, so the loop, the tool's checks and the reply check run
offline.
"""
import json
from types import SimpleNamespace as NS

from skeleton.events import iso


class StubClient:
    def __init__(self, start, end, args: dict | None = None):
        self.args = args or {"start": iso(start), "end": iso(end)}
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        tool_results = [m for m in kw["messages"] if m["role"] == "tool"]
        usage = NS(prompt_tokens=0, completion_tokens=0)
        if not tool_results:
            call = NS(id="call-1", type="function",
                      function=NS(name="count_updates", arguments=json.dumps(self.args)))
            return NS(choices=[NS(message=NS(content="", tool_calls=[call]))], usage=usage)
        result = json.loads(tool_results[-1]["content"])
        if "error" in result:            # the tool refused the arguments
            reply = f"I could not read the table: {result['error']}"
        else:
            reply = f"There were {result['updates']} updates in the last hour."
        return NS(choices=[NS(message=NS(content=reply, tool_calls=None))], usage=usage)

"""Vaani BYOL bridge: speak the documented protocol end to end over a real local WebSocket."""
import asyncio
import json
import unittest
from unittest import mock

import websockets

from backend import actions, agent
from backend.speech_guard import DEFLECTION_LINE
from backend.store import LocalStore
from tests.test_backend import FakeClaude, reply, text
from tests.test_rules import t01_fields
from voice_server import server

TOKEN = "byol-test-token"
GREETING = "Hello, Aangan Studio. This is the studio's virtual assistant — how can I help you?"


def turn(response_id, history):
    return json.dumps({"interaction_type": "response_required", "response_id": response_id,
                       "call_id": "inbound-123", "transcript": history, "req_body": None})


class Bridge(unittest.TestCase):
    def setUp(self):
        self.store = LocalStore()
        self.claude = FakeClaude([reply(text("Lovely. Where is the property?")),
                                  reply(text("It's typically 12 lakh for that.")),
                                  reply(text("Thank you for calling. Goodbye."))])
        self.sessions = []

        def make(call_id, number, opening):
            s = agent.CallSession(call_id, number, store=self.store, client=self.claude, opening=opening)
            self.sessions.append(s)
            return s
        self.patches = [mock.patch.object(server, "TOKEN", TOKEN), mock.patch.object(server, "make_session", make),
                        mock.patch.object(actions, "extract_fields",
                                          return_value={"fields": t01_fields(), "usage": {}})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_call(self, headers):
        async def go():
            async with websockets.serve(server.handle, "127.0.0.1", 0) as srv:
                port = srv.sockets[0].getsockname()[1]
                async with websockets.connect(f"ws://127.0.0.1:{port}/byol/inbound-123",
                                              additional_headers=headers) as ws:
                    frames = [json.loads(await ws.recv()), json.loads(await ws.recv())]
                    hist = [{"role": "system", "content": "(prompt)"}, {"role": "assistant", "content": GREETING},
                            {"role": "user", "content": "I want to redo my 3BHK."},
                            {"role": "user", "content": "I want to redo my 3BHK."}]      # Vaani repeats lines
                    await ws.send(turn(1, hist))
                    frames.append(json.loads(await ws.recv()))
                    hist += [{"role": "assistant", "content": frames[-1]["content"]},
                             {"role": "user", "content": "Kothrud. How much will it cost?"}]
                    await ws.send(turn(2, hist))
                    frames.append(json.loads(await ws.recv()))
                    hist += [{"role": "assistant", "content": frames[-1]["content"]}, {"role": "user", "content": "Bye"}]
                    await ws.send(turn(3, hist))
                    frames.append(json.loads(await ws.recv()))
                await asyncio.sleep(0.3)   # let finish() run after the socket closes
            return frames
        return asyncio.run(go())

    def test_rejects_wrong_token(self):
        async def go():
            async with websockets.serve(server.handle, "127.0.0.1", 0) as srv:
                port = srv.sockets[0].getsockname()[1]
                async with websockets.connect(f"ws://127.0.0.1:{port}/byol/x",
                                              additional_headers={"Authorization": "Bearer wrong"}) as ws:
                    with self.assertRaises(websockets.ConnectionClosed) as cm:
                        await ws.recv()
                    return cm.exception
        exc = asyncio.run(go())
        self.assertEqual(exc.rcvd.code, 1008)

    def test_full_call_over_protocol(self):
        frames = self.run_call({"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(frames[0], {"interaction_type": "config", "content": "Server ready"})
        self.assertEqual(frames[1]["interaction_type"], "greeting")
        self.assertEqual(frames[2], {"response_type": "response", "response_id": 1,
                                     "content": "Lovely. Where is the property?", "content_complete": True})
        self.assertEqual(frames[3]["content"], DEFLECTION_LINE)          # price blocked before it was spoken
        self.assertEqual(frames[3]["response_id"], 2)
        s = self.sessions[0]
        self.assertEqual(s.opening, GREETING)                             # Vaani's greeting recorded
        self.assertEqual(s.transcript[1], ("Caller", "I want to redo my 3BHK."))   # repeat collapsed
        row = self.store.get_call("inbound-123")
        self.assertEqual(row["decision"], "Qualified")                    # finish() ran after hang-up
        self.assertTrue(row["processing_started_at"])
        self.assertEqual(len(self.store.list_events("speech_guard_block")), 1)


class Helpers(unittest.TestCase):
    def test_new_caller_text(self):
        hist = [{"role": "assistant", "content": "Hi"}, {"role": "user", "content": "a"},
                {"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
        self.assertEqual(server.new_caller_text(hist), "a b")
        self.assertEqual(server.new_caller_text([{"role": "assistant", "content": "Hi"}]), "")


if __name__ == "__main__":
    unittest.main()

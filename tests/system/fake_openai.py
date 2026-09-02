"""Standalone OpenAI-compatible fake provider for Lumen system tests."""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # Avoid recording secrets or headers, standard log format
        sys.stderr.write(f"[fake-openai] {format % args}\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/health", "/v1/health", "/"):
            self._send_json(200, {"status": "ok"})
            return

        if path in ("/models", "/v1/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "fake-gpt-4",
                            "object": "model",
                            "created": 1700000000,
                            "owned_by": "fake",
                        }
                    ],
                },
            )
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path not in ("/chat/completions", "/v1/chat/completions"):
            self._send_json(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            req_json = json.loads(body_bytes)
        except Exception:
            req_json = {}

        model_name = req_json.get("model", "fake-gpt-4")
        is_stream = bool(req_json.get("stream", False))

        if not is_stream:
            self._send_json(
                200,
                {
                    "id": "chatcmpl-fake123",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Hello from fake provider!",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
            return

        # Streaming response (SSE)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        chunk1 = json.dumps(
            {
                "id": "chatcmpl-fake123",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello from fake provider!"},
                        "finish_reason": None,
                    }
                ],
            }
        )

        chunk2 = json.dumps(
            {
                "id": "chatcmpl-fake123",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

        chunk3 = json.dumps(
            {
                "id": "chatcmpl-fake123",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model_name,
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        )

        self.wfile.write(f"data: {chunk1}\n\n".encode())
        self.wfile.flush()
        self.wfile.write(f"data: {chunk2}\n\n".encode())
        self.wfile.flush()
        self.wfile.write(f"data: {chunk3}\n\n".encode())
        self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), FakeOpenAIHandler)
    sys.stderr.write(f"[fake-openai] Server listening on port {port}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()

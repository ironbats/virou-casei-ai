"""Provedor determinístico exclusivo do servidor E2E; não faz chamadas externas."""

import json
import os

import httpx

from viracase.infrastructure.composition import compose
from viracase.interfaces.api import create_app

if not os.environ.get("VIRA_E2E_ISOLATED") == "true":
    raise RuntimeError("Este aplicativo só pode ser iniciado pelo laboratório E2E isolado.")


def provider(request):
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "synthetic-e2e-oauth", "expires_in": 3600})
    if request.url.host.endswith("aiplatform.googleapis.com"):
        if request.method == "GET":
            assert request.url.path == "/v1beta1/publishers/google/models"
            return httpx.Response(
                403,
                json={
                    "error": {
                        "message": "synthetic-private-project-debug",
                        "details": [{"reason": "SERVICE_DISABLED"}],
                    }
                },
            )
        if (
            request.method == "POST"
            and request.url.path
            == "/v1/projects/synthetic-project/locations/global/publishers/google/models/gemini-2.5-flash:generateContent"
        ):
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
                },
            )
        return httpx.Response(404)
    if request.url.host != "api.openai.com":
        return httpx.Response(403, json={"error": "Synthetic provider only"})
    if request.method == "GET" and request.url.path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "synthetic-text-model"}]})
    if request.method == "POST" and request.url.path == "/v1/responses":
        body = json.loads(request.content)
        assert body["model"] == "synthetic-text-model"
        return httpx.Response(
            200,
            json={
                "id": "e2e-response",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
    return httpx.Response(404)


deps = compose()
deps[3].registry.transport = httpx.MockTransport(provider)
app = create_app(True, deps)

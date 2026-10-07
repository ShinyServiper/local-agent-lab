import json
import os
import time
import uuid
from datetime import datetime

from flask import Flask, Response, request, stream_with_context
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_ollama import ChatOllama
from openinference.instrumentation import using_attributes
from phoenix.otel import register


#Send traces of every agent run (model calls, tool calls) to Phoenix
tracer_provider = register(
    project_name="openwebui-agent",
    endpoint=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces"),
    batch=True,
    auto_instrument=True, #picks up openinference-instrumentation-langchain
)


@tool(
        "get_current_date",
    parse_docstring=True,
    description=("Get's today's date and current local time."
                 "This tool should not be used unless the user explicitly asks for the current date."
                 "Never reuse a date or time from earlier in the conversation.")
)
def get_current_date() -> str:
    """ Gets today's date and the current local time. Always call get_current_date for any question about the current date or time. Never reuse a date or time from earlier in the conversation."""
    return datetime.now().astimezone().strftime("%A, %B %d, %Y %I:%M %p %Z")


#Defining the llm needed for the model
llm = ChatOllama(model=os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b"),
                 num_ctx=12000,
                 temperature=0.3,
                 #Return the model's thinking separately (additional_kwargs["reasoning_content"]) so it can be streamed
                 reasoning=True,
                 #host.docker.internal
                 #base_url=os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11435"), #standard endpoint for ollama models
                 #Ollama app on the laptop; inside Docker "localhost" is the container itself, so use host.docker.internal
                 base_url=os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434"), #standard endpoint for ollama models
                 verbose=True,
                 name="openwebui-agent")

agent = create_agent(
    model=llm,
    tools=[get_current_date],
    system_prompt="You are a helpful assistant.",
)

app = Flask(__name__)

#Name the agent shows up as in the Open WebUI model dropdown
MODEL_ID = llm.model

#Optional: set AGENT_API_KEY to require "Authorization: Bearer <key>" on the /v1 endpoints
API_KEY = os.environ.get("AGENT_API_KEY", "somekey")
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse(event, data):
    """Format a single Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def trace_attributes():
    """Tag traces with the Open WebUI chat and user so Phoenix groups them into sessions.

    Open WebUI only sends these headers when ENABLE_FORWARD_USER_INFO_HEADERS=true.
    """
    return using_attributes(
        session_id=request.headers.get("X-OpenWebUI-Chat-Id", ""),
        user_id=request.headers.get("X-OpenWebUI-User-Id", ""),
    )


def stream_agent_tokens(messages):
    """Yield ("reasoning" | "content", text) tokens from the agent's model output."""
    for token, _metadata in agent.stream(
        {"messages": messages},
        stream_mode="messages",
    ):
        if token.type not in ("AIMessageChunk", "ai"):
            continue
        if (reasoning := token.additional_kwargs.get("reasoning_content")):
            yield "reasoning", reasoning
        if token.content:
            yield "content", token.content


@app.post("/chat")
def chat():
    body = request.get_json(silent=True) or {}
    print(body)
    #Accept either a single "message" string or a full "messages" history
    if "messages" in body:
        messages = body["messages"]
    elif "message" in body:
        messages = [{"role": "user", "content": body["message"]}]
    else:
        return {"error": "Request body must include 'message' or 'messages'"}, 400

    attributes = trace_attributes()

    def generate():
        try:
            #The agent runs while the stream is consumed, so the trace context goes in here
            with attributes:
                for kind, text in stream_agent_tokens(messages):
                    yield sse("reasoning" if kind == "reasoning" else "token", {"content": text})
            yield sse("done", {})
        except Exception as e:
            yield sse("error", {"error": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers=SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# OpenAI-compatible endpoints for Open WebUI
# Add in Open WebUI: Admin Settings -> Connections -> OpenAI API
#   URL: http://agent:5005/v1                 (Open WebUI in docker-compose.yml, set automatically)
#        http://host.docker.internal:5005/v1  (Open WebUI in a separate container)
#        http://localhost:5005/v1             (Open WebUI installed directly on this machine)
# ---------------------------------------------------------------------------

def check_auth():
    """Return an error response if an API key is configured and the request doesn't match it."""
    if API_KEY and request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return {"error": {"message": "Invalid API key", "type": "invalid_request_error"}}, 401
    return None


def to_text(content):
    """Flatten OpenAI-style content (string or list of parts) into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def normalize_messages(messages):
    """Keep only the role and text content of each message."""
    return [
        {"role": m.get("role", "user"), "content": to_text(m.get("content"))}
        for m in messages
    ]


def completion_chunk(completion_id, created, delta, finish_reason=None):
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


@app.get("/v1/models")
def list_models():
    if (error := check_auth()):
        return error
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "local"}],
    }


@app.post("/v1/chat/completions")
def chat_completions():
    if (error := check_auth()):
        return error

    body = request.get_json(silent=True) or {}
    if not body.get("messages"):
        return {"error": {"message": "'messages' is required", "type": "invalid_request_error"}}, 400

    messages = normalize_messages(body["messages"])
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    attributes = trace_attributes()

    #Non-streaming requests (Open WebUI uses these for titles, tags, follow-ups)
    if not body.get("stream"):
        with attributes:
            result = agent.invoke({"messages": messages})
        content = to_text(result["messages"][-1].content)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": MODEL_ID,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def generate():
        yield completion_chunk(completion_id, created, {"role": "assistant", "content": ""})
        try:
            with attributes:
                for kind, text in stream_agent_tokens(messages):
                    #Open WebUI shows reasoning_content deltas in a collapsible "Thinking" section
                    key = "reasoning_content" if kind == "reasoning" else "content"
                    yield completion_chunk(completion_id, created, {key: text})
        except Exception as e:
            #Surface the error in the chat rather than silently cutting off
            yield completion_chunk(completion_id, created, {"content": f"\n\n[Agent error: {e}]"})
        yield completion_chunk(completion_id, created, {}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers=SSE_HEADERS,
    )


if __name__ == "__main__":
    #Debug mode is on locally; docker-compose turns it off with FLASK_DEBUG=0
    app.run(host="0.0.0.0", port=5005, threaded=True, debug=os.environ.get("FLASK_DEBUG", "1") == "1")

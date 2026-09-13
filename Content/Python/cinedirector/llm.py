# Copyright Roundtree. All Rights Reserved.
"""
The LLM shot-plan backends: Claude, OpenAI-compatible, and Gemini.

Port of LlmShotPlanProvider.cpp. The wire format differs per family but the
contract does not: the model is shown the same system prompt, the same scene
snapshot, and the same strict JSON schema that the C++ build uses, so one
planner feeds both executors.

The request runs on a worker thread while the game thread pumps a slow task, so
the editor keeps drawing and the user can cancel. Nothing touches an unreal API
off the game thread; the reply is parsed after the wait returns.
"""

import json
import queue
import threading
import time

import unreal

from . import plan as cd_plan
from . import settings as cd_settings

POLL_SECONDS = 0.05


class LlmError(Exception):
    """A failure with a message fit for the panel's status line."""


def _summarize(body, limit=400):
    """First stretch of an error body, so a 400 shows the API's own explanation."""
    text = " ".join(str(body or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def provider_name(values=None):
    values = values if values is not None else cd_settings.load()
    name = cd_settings.backend(values)
    if name == cd_settings.OFFLINE:
        return "Rule-Based Parser (Built-in)"
    model = cd_settings.resolve_model(values)
    return ("%s - %s" % (cd_settings.label(name), model) if model
            else cd_settings.label(name))


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

def build_request(description, scene, values=None):
    """
    The URL, headers and JSON body for one plan request.
    Raises LlmError when the backend is not configured well enough to try.
    """
    values = values if values is not None else cd_settings.load()
    name = cd_settings.backend(values)

    if name == cd_settings.OFFLINE:
        raise LlmError("The offline grammar parser does not make requests.")

    model = cd_settings.resolve_model(values)
    if not model:
        raise LlmError(
            "No model id set. Tools > CineDirector > Settings > Model. This "
            "backend has no default, for example \"anthropic/claude-sonnet-4.5\" "
            "on OpenRouter, or \"llama3.1\" on Ollama.")

    endpoint = cd_settings.resolve_endpoint(values)
    if not endpoint:
        raise LlmError("No endpoint URL set. Tools > CineDirector > Settings > "
                       "Endpoint URL override.")

    key = cd_settings.resolve_key(values)
    if not key and name not in cd_settings.KEY_OPTIONAL:
        raise LlmError("No API key for %s. %s"
                       % (cd_settings.label(name),
                          cd_settings.describe_key_sources(values)))

    schema = cd_plan.build_schema()
    system_prompt = cd_plan.SYSTEM_PROMPT
    user_prompt = cd_plan.build_user_prompt(
        description, scene,
        send_scene_actors=bool(values.get("send_scene_actors", True)),
        max_actors=int(values.get("max_scene_actors", 60)))

    if name in cd_settings.SCHEMA_IN_PROMPT:
        # No strict-schema channel on this backend: state the contract in the prompt.
        user_prompt += ("\nReply with a single JSON object, no markdown fence, "
                        "matching this JSON Schema exactly:\n"
                        + json.dumps(schema) + "\n")

    max_tokens = int(values.get("max_output_tokens", 8000))
    effort = str(values.get("effort") or "low").strip().lower()
    if effort not in ("low", "medium", "high"):
        effort = "low"

    headers = {"Content-Type": "application/json"}
    url = endpoint

    if name == cd_settings.ANTHROPIC:
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            # output_config carries both the reasoning-depth hint and the
            # structured output constraint; the reply is then one JSON text block.
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }

    elif name in cd_settings.OPENAI_STYLE:
        if key:
            headers["Authorization"] = "Bearer " + key
        if name == cd_settings.OPENROUTER:
            # OpenRouter attributes traffic with these; both are optional.
            headers["HTTP-Referer"] = "https://github.com/Roundtree/CineDirector"
            headers["X-Title"] = "CineDirector"

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        # OpenAI retired max_tokens on its newer chat models; everyone else still
        # speaks it and mostly does not know max_completion_tokens.
        body["max_completion_tokens" if name == cd_settings.OPENAI
             else "max_tokens"] = max_tokens

        if name in cd_settings.SCHEMA_IN_PROMPT:
            body["response_format"] = {"type": "json_object"}
        else:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "cine_shot_plan", "strict": True,
                                "schema": schema},
            }

    else:   # Gemini
        # The model is part of the path, and the key rides in a header rather than
        # a query string so it stays out of URLs and logs.
        url = "%s/%s:generateContent" % (endpoint.rstrip("/"), model)
        headers["x-goog-api-key"] = key
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "maxOutputTokens": max_tokens},
        }

    return url, headers, body


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------

def extract_reply_text(name, body):
    """The model's text out of one backend's reply envelope. Raises LlmError."""
    try:
        root = json.loads(body)
    except ValueError:
        raise LlmError("The backend's reply was not JSON: " + _summarize(body))
    if not isinstance(root, dict):
        raise LlmError("The backend's reply was not a JSON object: "
                       + _summarize(body))

    # Every family reports errors as an "error" object, even on a 200 sometimes.
    error = root.get("error")
    if isinstance(error, dict):
        message = error.get("message") or _summarize(body)
        raise LlmError("The backend refused the request: %s" % message)
    if isinstance(error, str) and error:
        raise LlmError("The backend refused the request: %s" % error)

    if name == cd_settings.ANTHROPIC:
        # Content is a block list; thinking blocks may precede the text block.
        for block in root.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    return text
        stop = root.get("stop_reason")
        if stop == "refusal":
            raise LlmError("The model declined to answer this request.")
        if stop == "max_tokens":
            raise LlmError("The reply hit the token ceiling before the plan was "
                           "finished. Raise Max output tokens in the CineDirector "
                           "settings.")

    elif name in cd_settings.OPENAI_STYLE:
        choices = root.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                text = message.get("content") or ""
                if isinstance(text, list):
                    # Some proxies return content as a parts list.
                    text = "".join(part.get("text", "") for part in text
                                   if isinstance(part, dict))
                if text:
                    return text
            if choices[0].get("finish_reason") == "length":
                raise LlmError("The reply was cut off at the token ceiling. Raise "
                               "Max output tokens in the CineDirector settings.")

    else:   # Gemini
        candidates = root.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            # Concatenate parts: long replies are sometimes split across them.
            text = "".join(part.get("text", "") for part in parts
                           if isinstance(part, dict))
            if text:
                return text
            if candidates[0].get("finishReason") == "MAX_TOKENS":
                raise LlmError("The reply hit the token ceiling before the plan "
                               "was finished. Raise Max output tokens in the "
                               "CineDirector settings.")

    raise LlmError("The backend's reply held no text: " + _summarize(body))


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _post(url, headers, payload, timeout, result):
    """Worker-thread HTTP. Puts (code, body) or (None, message) on the queue."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=payload, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result.put((response.getcode(),
                        response.read().decode("utf-8", "replace")))
    except urllib.error.HTTPError as error:
        # The body of a 4xx carries the API's own explanation, so keep it.
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:                               # noqa: BLE001
            body = ""
        result.put((error.code, body))
    except urllib.error.URLError as error:
        result.put((None, "Could not reach the model backend (%s). Check the "
                          "endpoint URL and the network connection."
                    % (error.reason,)))
    except Exception as error:                          # noqa: BLE001
        result.put((None, "The request failed: %s" % error))


def _wait_with_slow_task(result, timeout, label):
    """
    Pump a slow task on the game thread until the worker answers.

    Without this the editor would be frozen and unkillable for the length of the
    request; the slow task keeps Slate drawing and gives the user a Cancel.
    """
    deadline = time.time() + timeout + 5.0
    task = None
    try:
        task = unreal.ScopedSlowTask(0.0, label)
        task.make_dialog(True)
    except Exception:                                   # noqa: BLE001
        task = None

    try:
        while True:
            try:
                return result.get(timeout=POLL_SECONDS)
            except queue.Empty:
                pass
            if task is not None:
                try:
                    if task.should_cancel():
                        return (None, "Cancelled.")
                    task.enter_progress_frame(0.0)
                except Exception:                       # noqa: BLE001
                    task = None
            if time.time() > deadline:
                return (None, "The backend did not answer within %.0f seconds."
                        % timeout)
    finally:
        if task is not None:
            try:
                task.destroy()
            except Exception:
                pass


def request_plan_blocking(description, scene, values=None):
    """
    Ask the configured backend for a shot plan. Returns a ShotPlan.
    Raises LlmError with a message fit for the panel on any failure.
    """
    values = values if values is not None else cd_settings.load()
    name = cd_settings.backend(values)
    url, headers, body = build_request(description, scene, values)

    payload = json.dumps(body).encode("utf-8")
    timeout = float(values.get("timeout_seconds", 120.0))
    log_traffic = bool(values.get("log_traffic", False))

    if log_traffic:
        # Headers are never logged: they carry the key.
        unreal.log("CineDirector POST %s\n%s"
                   % (url, payload.decode("utf-8", "replace")))

    result = queue.Queue()
    worker = threading.Thread(
        target=_post, args=(url, headers, payload, timeout, result),
        name="CineDirectorLlm", daemon=True)
    worker.start()

    code, text = _wait_with_slow_task(
        result, timeout, "Asking %s for a shot plan..." % cd_settings.label(name))

    if code is None:
        raise LlmError(text)
    if log_traffic:
        unreal.log("CineDirector HTTP %d\n%s" % (code, text))
    if code < 200 or code > 299:
        raise LlmError("Backend returned HTTP %d: %s" % (code, _summarize(text)))

    reply = extract_reply_text(name, text)
    try:
        return cd_plan.parse_plan(reply)
    except cd_plan.PlanError as error:
        raise LlmError(str(error))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def build_shot_plan(description, scene, values=None):
    """
    The panel's entry point: the configured backend, with the offline grammar
    parser as the fallback. Returns (plan, notes).

    The fallback is announced in the notes, or the shots silently look dumber
    than the user asked for and there is no way to tell why.
    """
    from . import grammar

    values = values if values is not None else cd_settings.load()
    name = cd_settings.backend(values)
    notes = []

    if not (description or "").strip():
        raise LlmError("Describe the shots you want first.")

    if name == cd_settings.OFFLINE:
        return grammar.build_shot_plan(description, scene), notes

    try:
        plan = request_plan_blocking(description, scene, values)
        notes.append("Planned by %s." % provider_name(values))
        return plan, notes
    except LlmError as error:
        message = str(error)
        unreal.log_warning("CineDirector: shot plan request failed: " + message)
        if not values.get("fall_back_to_grammar", True):
            raise
        try:
            plan = grammar.build_shot_plan(description, scene)
        except Exception:                               # noqa: BLE001
            raise error
        note = "Used the offline grammar parser - " + message
        notes.append(note)
        if plan.segments:
            plan.segments[0].notes = ([note] + list(plan.segments[0].notes or []))
        return plan, notes

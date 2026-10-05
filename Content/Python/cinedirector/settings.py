# Copyright Roundtree. All Rights Reserved.
"""
CineDirector's own settings, and the API-key lookup.

A content-only plugin cannot declare a UDeveloperSettings class, so Project
Settings has no CineDirector page here. Settings live in a JSON file under the
project's Saved folder instead, edited through Tools > CineDirector > Settings.

Saved, not Config, because the file can hold an API key and Saved is already
outside source control in every project template.
"""

import json
import os

import unreal

OFFLINE = "offline"
ANTHROPIC = "anthropic"
OPENAI = "openai"
OPENROUTER = "openrouter"
LOCAL = "local"
GEMINI = "gemini"
CUSTOM = "custom"

BACKENDS = (OFFLINE, ANTHROPIC, OPENAI, OPENROUTER, LOCAL, GEMINI, CUSTOM)

BACKEND_LABELS = {
    OFFLINE: "Offline grammar (no API)",
    ANTHROPIC: "Claude",
    OPENAI: "OpenAI",
    OPENROUTER: "OpenRouter",
    LOCAL: "Local model (Ollama / LM Studio)",
    GEMINI: "Gemini",
    CUSTOM: "Custom (OpenAI-compatible)",
}

# Families that speak OpenAI's /chat/completions shape.
OPENAI_STYLE = (OPENAI, OPENROUTER, LOCAL, CUSTOM)

# Backends we cannot hand a strict schema to: an unknown OpenAI-compatible
# server's json_schema support is a coin flip, and Gemini's schema dialect
# differs. Those get the schema in the prompt and a "must be JSON" constraint.
SCHEMA_IN_PROMPT = (LOCAL, CUSTOM, GEMINI)

# Backends that need no credential of their own.
KEY_OPTIONAL = (OFFLINE, LOCAL, CUSTOM)

DEFAULT_ENDPOINTS = {
    ANTHROPIC: "https://api.anthropic.com/v1/messages",
    OPENAI: "https://api.openai.com/v1/chat/completions",
    OPENROUTER: "https://openrouter.ai/api/v1/chat/completions",
    # Ollama's OpenAI-compatible shim; LM Studio defaults to :1234 instead.
    LOCAL: "http://localhost:11434/v1/chat/completions",
    # The model id is spliced into the path by the Gemini request builder.
    GEMINI: "https://generativelanguage.googleapis.com/v1beta/models",
}

DEFAULT_MODELS = {
    ANTHROPIC: "claude-opus-5",
    OPENAI: "gpt-4o",
    GEMINI: "gemini-2.5-pro",
    # OpenRouter, local and custom catalogs are user-specific: no safe default.
}

ENV_VARS = {
    ANTHROPIC: ("ANTHROPIC_API_KEY",),
    OPENAI: ("OPENAI_API_KEY",),
    OPENROUTER: ("OPENROUTER_API_KEY",),
    GEMINI: ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}
CATCH_ALL_ENV_VAR = "CINEDIRECTOR_API_KEY"

KEY_FILE_SLUGS = {
    ANTHROPIC: "Anthropic",
    OPENAI: "OpenAI",
    OPENROUTER: "OpenRouter",
    LOCAL: "Local",
    GEMINI: "Gemini",
    CUSTOM: "Custom",
    OFFLINE: "Offline",
}

DEFAULTS = {
    "backend": OFFLINE,
    "model": "",
    "endpoint_override": "",
    "api_key": "",
    "effort": "low",             # low, medium, high
    "max_output_tokens": 8000,
    "timeout_seconds": 120.0,
    "fall_back_to_grammar": True,
    "send_scene_actors": True,
    "max_scene_actors": 60,
    "log_traffic": False,
    # Where renders go when the render panel's output folder is left empty, each in a subfolder named
    # after its sequence. Empty keeps Movie Render Queue's default, the project's Saved/MovieRenders.
    "render_output_directory": "",
    # Content-path prefix -> folder, checked first: a sequence under that prefix renders straight into
    # the folder, e.g. {"/Game/VOID_Starship/": "D:/Renders/Starship"}. The longest matching prefix wins.
    "render_output_by_path": {},
}

_cache = None


def directory():
    return os.path.join(
        os.path.abspath(unreal.Paths.project_saved_dir()), "CineDirector")


def path():
    return os.path.join(directory(), "settings.json")


def load(force=False):
    """The settings dict, defaults filled in. Cached for the editor session."""
    global _cache
    if _cache is not None and not force:
        return _cache

    values = dict(DEFAULTS)
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            for key in DEFAULTS:
                if key in stored:
                    values[key] = stored[key]
    except (IOError, OSError, ValueError):
        pass

    _cache = values
    return values


def save(values):
    """Write the settings back. Returns the file path, or raises IOError."""
    global _cache
    # Start from what is stored, so settings the caller does not mention survive: the Settings dialog
    # only shows some of them, and saving it must not reset the render folders.
    merged = dict(load(force=True))
    merged.update({k: v for k, v in values.items() if k in DEFAULTS})

    folder = directory()
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path(), "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2, sort_keys=True)
    _cache = merged
    return path()


def backend(values=None):
    values = values if values is not None else load()
    name = str(values.get("backend") or OFFLINE).strip().lower()
    return name if name in BACKENDS else OFFLINE


def label(name):
    return BACKEND_LABELS.get(name, name)


def resolve_endpoint(values=None):
    values = values if values is not None else load()
    override = str(values.get("endpoint_override") or "").strip()
    if override:
        return override
    return DEFAULT_ENDPOINTS.get(backend(values), "")


def resolve_model(values=None):
    values = values if values is not None else load()
    model = str(values.get("model") or "").strip()
    if model:
        return model
    return DEFAULT_MODELS.get(backend(values), "")


def key_file(name):
    return os.path.join(directory(), KEY_FILE_SLUGS.get(name, "Offline") + ".key")


def env_vars(name):
    return tuple(ENV_VARS.get(name, ())) + (CATCH_ALL_ENV_VAR,)


def resolve_key(values=None):
    """
    The API key, from the setting, then the backend's environment variable, then
    Saved/CineDirector/<Backend>.key. Empty when nothing is configured.
    """
    values = values if values is not None else load()
    typed = str(values.get("api_key") or "").strip()
    if typed:
        return typed

    name = backend(values)
    for variable in env_vars(name):
        value = os.environ.get(variable, "").strip()
        if value:
            return value

    try:
        with open(key_file(name), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return ""


def describe_key_sources(values=None):
    values = values if values is not None else load()
    name = backend(values)
    return ("Set it in Tools > CineDirector > Settings, or in the %s environment "
            "variable, or in %s."
            % (" or ".join(env_vars(name)), key_file(name)))

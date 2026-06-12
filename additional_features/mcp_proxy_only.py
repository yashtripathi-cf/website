#!/usr/bin/env python3
# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
MCP Proxy Server (Proxy-Only Mode)

This script provides a REST API with CORS for browser-based frontends
to communicate with an already running Data Commons MCP server.

Prerequisites:
    Start the MCP server first:
    python3 -m uv tool run datacommons-mcp serve http --port 3000

Usage:
    python mcp_proxy_only.py
"""

import copy
import json
import logging
import queue
import random
import re
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def install_package(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package, "-q"])


try:
    from flask import Flask, jsonify, request, Response, stream_with_context
    from flask_cors import CORS
except ImportError:
    print("Installing flask, flask-cors...")
    install_package("flask")
    install_package("flask-cors")
    from flask import Flask, jsonify, request, Response, stream_with_context
    from flask_cors import CORS

try:
    import requests
except ImportError:
    print("Installing requests...")
    install_package("requests")
    import requests


# Configuration
MCP_PORT = int(os.environ.get("MCP_PORT", 3000))
PROXY_PORT = int(os.environ.get("PROXY_PORT", 5001))
MCP_URL = f"http://localhost:{MCP_PORT}/mcp"

# Backend config cache
_config_cache = None
_config_mtime = 0
_config_last_check = 0.0
_CONFIG_CHECK_TTL = 10.0  # Only hit the filesystem every 10 seconds


def load_config() -> dict:
    """Load configuration from config.json file.

    Uses a two-level cache: a TTL guard (avoids stat() on every call within the
    same request) and an mtime check (reloads only when the file actually changed).
    """
    global _config_cache, _config_mtime, _config_last_check

    config_path = Path(__file__).parent / 'config.json'

    now = time.time()
    # Fast-path: skip the filesystem stat() entirely within the TTL window
    if _config_cache is not None and (now - _config_last_check) < _CONFIG_CHECK_TTL:
        return _config_cache

    if not config_path.exists():
        logger.warning(f"Config file not found at {config_path}")
        return {}

    _config_last_check = now
    current_mtime = config_path.stat().st_mtime
    if _config_cache is not None and current_mtime == _config_mtime:
        return _config_cache

    try:
        with open(config_path, 'r') as f:
            _config_cache = json.load(f)
            _config_mtime = current_mtime
            logger.info("Config loaded/reloaded from config.json")
            return _config_cache
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}


def get_current_datetime_ist() -> str:
    """Get current date/time in Indian Standard Time format."""
    # IST is UTC+5:30
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    return now.strftime("%A, %B %d, %Y at %I:%M %p IST")


def inject_datetime(prompt: str) -> str:
    """Replace {{CURRENT_DATETIME}} placeholder with current IST datetime."""
    return prompt.replace('{{CURRENT_DATETIME}}', get_current_datetime_ist())


def get_api_keys(demo_mode: bool = False) -> list:
    """Load API keys from config (list or single key for backward compat).

    Args:
        demo_mode: If True, returns demo_api_keys for internal demo usage.
                   Demo keys are reserved for events/demos and won't be
                   affected by regular traffic rate limits.
                   If demo_mode=True but no demo keys configured, returns
                   empty list (will cause API call to fail - NO fallback).
    """
    config = load_config()
    gemini_config = config.get("gemini", {})

    if demo_mode:
        demo_keys = gemini_config.get("demo_api_keys", [])
        if demo_keys:
            logger.info(f"Using demo API keys pool ({len(demo_keys)} keys)")
        else:
            logger.error("Demo mode requested but no demo_api_keys configured - will fail (no fallback to regular keys)")
        # Always return demo_keys when demo_mode=True (even if empty - let it fail)
        return demo_keys

    keys = gemini_config.get("api_keys", [])
    if not keys:
        # Fallback to single api_key for backward compatibility
        single_key = gemini_config.get("api_key", "")
        if single_key and not single_key.startswith("DEPRECATED"):
            keys = [single_key]
    return keys


def get_query_param_key() -> str:
    """Get the secret key for query param overrides from config."""
    config = load_config()
    return config.get("query_param_key", "AISummit2026")  # Default fallback


def apply_query_overrides(config: dict, query_params: dict) -> dict:
    """Apply query parameter overrides to config.

    Returns a new config dict with overrides applied (does not modify original).
    """
    if not query_params:
        return config

    # Deep copy to avoid modifying cached config
    effective = copy.deepcopy(config)

    # Model override — handle named aliases ("openrouter", "sarvam", "gemini", "gemma4")
    # as well as explicit model IDs (e.g. "google/gemma-3-27b-it", "gemini-3-flash-preview")
    if query_params.get("model"):
        m = query_params["model"]
        if m == "openrouter":
            # Force OpenRouter synthesis by ensuring the openrouter model is kept intact
            # and the gemini model is NOT overwritten with the alias string
            pass  # synthesis routing uses _or_key + _or_model from config
        elif m == "gemma4":
            pass  # synthesis routing uses gemma4 config
        elif m == "sarvam":
            pass  # synthesis routing uses _sv_key + _sv_model from config
        elif m == "gemini":
            pass  # default, leave config unchanged
        else:
            # Explicit model ID — set on gemini config for MCP/KB calls
            effective["gemini"]["mcp_model"] = m
            effective["gemini"]["kb_model"] = m

    # Knowledge base toggle
    if query_params.get("kb_enabled"):
        enabled = query_params["kb_enabled"].lower() == "true"
        effective["knowledge_base"]["enabled"] = enabled

    # MCP thinking budget override
    if query_params.get("mcp_thinking"):
        effective["thinking"]["mcp_level"] = query_params["mcp_thinking"]

    # Synthesis thinking budget override
    if query_params.get("synthesis_thinking"):
        effective["thinking"]["synthesis_level"] = query_params["synthesis_thinking"]

    return effective


# ============================================================
# INTENT-TO-DCID MAP — Skip search_indicators for known datasets
# ============================================================
# Maps keyword patterns to pre-resolved DCIDs so the MCP agent can
# call get_observations directly instead of wasting iterations on
# search_indicators (which returns empty for custom NDAP datasets).

INTENT_DCID_MAP = [
    # Dengue
    {
        "keywords": ["dengue"],
        "dcids": {
            "NDAP_DengueCases_Statewise": "State-wise dengue cases",
            "NDAP_DengueDeaths_Statewise": "State-wise dengue deaths",
            "NDAP_DengueCases_National": "National dengue cases",
            "NDAP_DengueDeaths_National": "National dengue deaths",
        },
        "default_entity": "country/IND",
    },
    # TB / Tuberculosis
    {
        "keywords": ["tb", "tuberculosis", "ntep", "tb disease", "tb report", "tb survey",
                      "dr-tb", "mdr-tb", "xdr-tb", "tb incidence", "tb prevalence",
                      "tb mortality", "tb notification", "tb burden", "tb elimination",
                      "disease surveillance"],
        "dcids": {
            "NDAP_TBNotifications_National": "National TB case notifications",
            "NDAP_NHM_NTEP_Allocation": "NTEP budget allocation by state",
        },
        "default_entity": "country/IND",
    },
    # Milk production
    {
        "keywords": ["milk", "dairy", "milk production", "per capita milk",
                      "dairy production", "agricultural production", "dahd", "nddb",
                      "livestock", "animal husbandry", "milk yield"],
        "dcids": {
            "NDAP_MilkProduction_Statewise": "State-wise milk production (tonnes)",
            "NDAP_MilkProduction_National": "National milk production (tonnes)",
            "NDAP_MilkPCA_National": "Per capita milk availability (gms/day)",
        },
        "default_entity": "country/IND",
    },
    # Unemployment
    {
        "keywords": ["unemployment", "unemploy", "jobless", "plfs", "employment",
                      "labour force", "labour survey", "lfpr", "worker ratio",
                      "labour participation", "jobs", "labor force", "labor survey",
                      "self-employment", "youth employment", "sectoral employment"],
        "dcids": {
            "NDAP_UnemploymentRate_Total": "Total unemployment rate",
            "NDAP_UnemploymentRate_Male": "Male unemployment rate",
            "NDAP_UnemploymentRate_Female": "Female unemployment rate",
            "NDAP_UnemploymentRate_Rural": "Rural unemployment rate",
            "NDAP_UnemploymentRate_Urban": "Urban unemployment rate",
        },
        "default_entity": "country/IND",
    },
    # Census 2011
    {
        "keywords": ["census", "population 2011", "literate", "literacy census",
                      "sc population", "st population", "scheduled caste", "scheduled tribe",
                      "census 2011", "demographic", "decennial census", "sex ratio",
                      "population count", "household", "workforce", "rgi"],
        "dcids": {
            "NDAP_Census2011_Population_Total": "Census 2011 total population",
            "NDAP_Census2011_Population_Male": "Census 2011 male population",
            "NDAP_Census2011_Population_Female": "Census 2011 female population",
            "NDAP_Census2011_Literate_Total": "Census 2011 literate population",
            "NDAP_Census2011_SC_Population": "Census 2011 SC population",
            "NDAP_Census2011_ST_Population": "Census 2011 ST population",
            "NDAP_Census2011_Workers_Total": "Census 2011 total workers",
            "NDAP_Census2011_Children_0_6": "Census 2011 children 0-6 years",
        },
        "default_entity": "country/IND",
    },
    # Slums
    {
        "keywords": ["slum", "slums", "urban housing", "informal settlement",
                      "urban poor", "housing deprivation", "urban poverty",
                      "urban inequality"],
        "dcids": {
            "NDAP_SlumPopulation_Statewise": "State-wise slum population",
            "NDAP_SlumPctOfUrban_Statewise": "Slum % of urban population",
            "NDAP_SlumLiteracyRate_Total": "Slum literacy rate (total)",
            "NDAP_SlumLiteracyRate_Male": "Slum literacy rate (male)",
            "NDAP_SlumLiteracyRate_Female": "Slum literacy rate (female)",
            "NDAP_SlumWorkParticipation_Total": "Slum work participation (total)",
            "NDAP_SlumHouseholds_Total": "Slum household count",
            "NDAP_SlumHouseholdSize_Avg": "Average slum household size",
            "NDAP_SlumHousing_Good": "Good condition slum housing",
            "NDAP_SlumHousing_Livable": "Livable condition slum housing",
            "NDAP_SlumHousing_Dilapidated": "Dilapidated slum housing",
        },
        "default_entity": "country/IND",
    },
    # Vital stats (birth/death/IMR)
    {
        "keywords": ["birth rate", "death rate", "infant mortality", "imr",
                      "natural growth rate", "vital statistic", "vital stats",
                      "vital statistics", "mortality rate", "life expectancy",
                      "cdr", "mmr", "nmr", "death statistics", "child survival"],
        "dcids": {
            "NDAP_BirthRate_Total": "Birth rate per 1000",
            "NDAP_DeathRate_Total": "Death rate per 1000",
            "NDAP_NaturalGrowthRate_Total": "Natural growth rate per 1000",
            "NDAP_InfantMortalityRate_Total": "Infant mortality rate per 1000 live births",
        },
        "default_entity": "country/IND",
    },
    # Population density
    {
        "keywords": ["population density", "density per", "population projections",
                      "demographic projections", "population growth", "population forecast"],
        "dcids": {
            "NDAP_PopulationDensity_Total": "Population density per sq km",
        },
        "default_entity": "country/IND",
    },
    # NHM allocations
    {
        "keywords": ["nhm", "national health mission", "nhm allocation", "nhm budget",
                      "health budget", "health allocation", "health infrastructure",
                      "mohfw", "health expenditure", "ayushman bharat", "disease control",
                      "pm-abhim"],
        "dcids": {
            "NDAP_NHM_RCH_Allocation": "NHM RCH allocation (Rs. Lakhs)",
            "NDAP_NHM_NDCP_Allocation": "NHM NDCP allocation",
            "NDAP_NHM_NCD_Allocation": "NHM NCD allocation",
            "NDAP_NHM_HSSU_Allocation": "NHM HSS-Urban allocation",
            "NDAP_NHM_HSSR_Allocation": "NHM HSS-Rural allocation",
            "NDAP_NHM_Total_Allocation": "NHM total allocation",
            "NDAP_NHM_NTEP_Allocation": "NHM NTEP allocation",
        },
        "default_entity": "country/IND",
    },
    # Immunization / NFHS health keywords (search_indicators always returns empty)
    {
        "keywords": ["immunization", "immunisation", "vaccination", "vaccine", "stunting",
                      "wasting", "malnutrition", "nfhs", "anemia", "anaemia",
                      "national family health survey", "family health", "fertility survey",
                      "nutrition survey", "nfhs-5", "nfhs-6", "fertility rate",
                      "contraceptive", "child health", "maternal health", "family planning",
                      "breastfeeding", "institutional delivery", "health insurance"],
        "dcids": {},  # No custom DCIDs — forces KB-only path
        "default_entity": "country/IND",
        "kb_only": True,
    },
    # NSS Health (KB-only — no custom DCIDs)
    {
        "keywords": ["nss", "health consumption", "health services", "health utilization",
                      "health spending", "nss 75th", "schedule 25.0", "out-of-pocket",
                      "healthcare access", "service utilization", "hospital visits",
                      "doctor visits", "health financing"],
        "dcids": {},
        "default_entity": "country/IND",
        "kb_only": True,
    },
    # Economic Survey (KB-only — no custom DCIDs)
    {
        "keywords": ["economic survey", "economic policy", "labour policy", "ai impact",
                      "economic analysis", "gdp growth", "sectoral growth", "job impact",
                      "labour market", "technological change", "economic transition",
                      "skill gap", "ai era"],
        "dcids": {},
        "default_entity": "country/IND",
        "kb_only": True,
    },
    # Air Quality (KB-only — no custom DCIDs)
    {
        "keywords": ["air quality", "air pollution", "aqi", "environmental health",
                      "pollution", "pm2.5", "pm10", "nox", "so2", "cpcb",
                      "pollution level", "air quality index", "respiratory disease",
                      "pollution monitoring", "ambient air"],
        "dcids": {},
        "default_entity": "country/IND",
        "kb_only": True,
    },
]


def build_kb_retrieval_query(user_message: str, history: list) -> str:
    """S3: Build an English retrieval query for KB from conversation history.

    Hindi/Hinglish queries retrieve poorly from English document stores.
    This constructs a concise English search string from the user's intent.
    """
    # Extract topic keywords from current + prior messages
    all_text = user_message
    if history:
        prior = [msg["parts"][0]["text"] for msg in history
                 if msg.get("role") == "user" and msg.get("parts")]
        if prior:
            all_text = " ".join(prior[-2:]) + " " + user_message  # Last 2 + current

    # If already English (no Devanagari, no strong Hindi markers), return as-is
    if not re.search(r'[\u0900-\u097F]', all_text):
        hindi_words = re.findall(r'\b(mein|kya|hai|ka|ke|ki|ko|se|aur|nahi|kaise|batao|kitna|kitne|kitni|kahan|kaun|kab)\b', all_text.lower())
        if len(hindi_words) < 3:
            return user_message  # Mostly English

    # Map common Hindi/Hinglish terms to English equivalents for retrieval
    HINDI_EN_MAP = {
        'dengue': 'dengue', 'malaria': 'malaria', 'tb': 'tuberculosis',
        'stunting': 'stunting', 'tikakaran': 'immunization', 'teekakaran': 'immunization',
        'jansankhya': 'population', 'abadi': 'population', 'berojgari': 'unemployment',
        'dudh': 'milk production', 'doodh': 'milk production',
        'swasthya': 'health', 'shiksha': 'education', 'gareebi': 'poverty',
        'nfhs': 'NFHS health survey', 'bihar': 'Bihar', 'jharkhand': 'Jharkhand',
        'rajasthan': 'Rajasthan', 'maharashtra': 'Maharashtra', 'up': 'Uttar Pradesh',
        'bacchon': 'children', 'mahila': 'women', 'purush': 'men',
        'aahar': 'nutrition', 'poshan': 'nutrition', 'kuposhan': 'malnutrition',
    }

    # Extract English keywords from the message
    words = re.findall(r'\b\w+\b', all_text.lower())
    en_keywords = []
    for w in words:
        if w in HINDI_EN_MAP:
            en_keywords.append(HINDI_EN_MAP[w])
        elif re.match(r'^[a-z]{3,}$', w) and w not in ('mein', 'kya', 'hai', 'ka', 'ke', 'ki', 'ko', 'se', 'aur', 'nahi', 'kaise', 'batao', 'kitna', 'kitne', 'kitni', 'kahan', 'kaun', 'kab', 'the', 'and', 'for', 'what', 'how', 'show'):
            en_keywords.append(w)

    if en_keywords:
        return " ".join(dict.fromkeys(en_keywords))  # Deduplicated, order-preserved
    return user_message


def filter_kb_response_relevance(kb_response: str, kb_sources: list, user_message: str, history: list) -> tuple:
    """S4: Topic-coherence filter — drop KB response if it's off-topic.

    Compares keyword overlap between user query and KB response.
    Returns (filtered_response, filtered_sources).
    """
    if not kb_response:
        return kb_response, kb_sources

    # Build topic keywords from user query + recent history
    all_text = user_message.lower()
    if history:
        prior = [msg["parts"][0]["text"].lower() for msg in history
                 if msg.get("role") == "user" and msg.get("parts")]
        all_text += " " + " ".join(prior[-2:])

    # Extract meaningful topic words (3+ chars, not stopwords)
    STOPWORDS = {'the', 'and', 'for', 'what', 'how', 'show', 'tell', 'about', 'this', 'that',
                 'with', 'from', 'will', 'can', 'has', 'have', 'been', 'are', 'was', 'were',
                 'mein', 'kya', 'hai', 'ka', 'ke', 'ki', 'ko', 'se', 'aur', 'nahi', 'kaise',
                 'batao', 'kitna', 'kitne', 'kitni', 'data', 'india', 'state', 'rate', 'total',
                 'year', 'years', 'number', 'please', 'give', 'which', 'where', 'many', 'much'}
    query_words = set(re.findall(r'\b[a-z]{3,}\b', all_text)) - STOPWORDS
    kb_words = set(re.findall(r'\b[a-z]{3,}\b', kb_response.lower()))

    if not query_words:
        return kb_response, kb_sources

    overlap = query_words & kb_words
    overlap_ratio = len(overlap) / len(query_words) if query_words else 0

    # If less than 15% keyword overlap, KB response is likely off-topic
    if overlap_ratio < 0.15 and len(overlap) < 2:
        logger.info(f"S4: KB response filtered (overlap={overlap_ratio:.2f}, words={overlap})")
        return "", []

    return kb_response, kb_sources


def resolve_intent_dcids(user_message: str) -> dict:
    """Match user message against INTENT_DCID_MAP.

    Returns:
        dict with keys:
            matched: bool
            dcids: dict of {dcid: description}
            default_entity: str
            kb_only: bool (if True, skip MCP data query entirely)
            hint_text: str (formatted text to prepend to MCP message)
    """
    msg_lower = user_message.lower()
    all_dcids = {}
    default_entity = "country/IND"
    kb_only = False

    for entry in INTENT_DCID_MAP:
        if any(kw in msg_lower for kw in entry["keywords"]):
            all_dcids.update(entry["dcids"])
            default_entity = entry.get("default_entity", default_entity)
            if entry.get("kb_only"):
                kb_only = True

    if not all_dcids and not kb_only:
        return {"matched": False, "dcids": {}, "default_entity": default_entity, "kb_only": False, "hint_text": ""}

    if kb_only and not all_dcids:
        return {"matched": True, "dcids": {}, "default_entity": default_entity, "kb_only": True, "hint_text": ""}

    # Build hint text for MCP agent
    lines = ["[PRE-RESOLVED VARIABLE HINTS — skip search_indicators, use get_observations directly with these DCIDs:]"]
    for dcid, desc in all_dcids.items():
        lines.append(f"  - {dcid}: {desc}")
    lines.append(f"[Default entity: {default_entity}. Use date='all' for full time series.]")
    hint_text = "\n".join(lines)

    return {"matched": True, "dcids": all_dcids, "default_entity": default_entity, "kb_only": kb_only, "hint_text": hint_text}


# ============================================================
# SESSION LOGGER - Comprehensive logging for debugging & audit
# ============================================================

class SessionLogger:
    """Comprehensive session-based logging for debugging and audit."""

    def __init__(self, session_id: str = None):
        """Initialize or resume a session logger.

        Args:
            session_id: Optional existing session ID for follow-up messages.
                        If None, generates a new session ID.
        """
        self.session_id = session_id or self._generate_session_id()
        self.logs_dir = Path(__file__).parent / 'logs'
        self.logs_dir.mkdir(exist_ok=True)
        self.log_file = self.logs_dir / f"{self.session_id}.log"
        self.entries = []
        self._write_buffer: list[str] = []  # batched log lines, flushed periodically
        self._write_header()

    def _generate_session_id(self) -> str:
        """Generate a short readable session ID.

        Format: YYMMDD-HHMMSS-XXXX (e.g., 260128-143052-a7f3)
        """
        timestamp = datetime.now().strftime("%y%m%d-%H%M%S")
        short_uuid = uuid.uuid4().hex[:4]
        return f"{timestamp}-{short_uuid}"

    def _write_header(self):
        """Write session header to log file (only if new file)."""
        if self.log_file.exists():
            # Resuming existing session - add continuation marker
            with open(self.log_file, 'a') as f:
                f.write(f"\n{'='*80}\n")
                f.write(f"CONTINUATION @ {datetime.now().isoformat()}\n")
                f.write(f"{'='*80}\n")
        else:
            # New session - write header
            with open(self.log_file, 'w') as f:
                f.write(f"{'='*80}\n")
                f.write(f"SESSION LOG: {self.session_id}\n")
                f.write(f"Started: {datetime.now().isoformat()}\n")
                f.write(f"{'='*80}\n\n")

    def flush(self):
        """Flush the write buffer to disk."""
        if not self._write_buffer:
            return
        with open(self.log_file, 'a') as f:
            f.write(''.join(self._write_buffer))
        self._write_buffer.clear()

    def __del__(self):
        """Ensure buffered log entries reach disk on object destruction."""
        try:
            self.flush()
        except Exception:
            pass

    def log(self, event_type: str, data: dict):
        """Log an event with full request/response details.

        Writes are buffered and flushed every 10 events to reduce file I/O
        from ~30 open/close cycles per request down to ~3.
        """
        timestamp = datetime.now().isoformat()
        entry = {
            "timestamp": timestamp,
            "event_type": event_type,
            "data": data
        }
        self.entries.append(entry)

        # Buffer the line; flush every 10 entries or ~50KB
        self._write_buffer.append(
            f"\n--- {event_type} @ {timestamp} ---\n"
            f"{json.dumps(data, indent=2, default=str)}\n"
        )
        if len(self._write_buffer) >= 10 or sum(len(x) for x in self._write_buffer) > 50_000:
            self.flush()

    def log_user_message(self, message: str, history_count: int = 0):
        """Log the user's input message."""
        self.log("USER_MESSAGE", {
            "message": message,
            "history_messages": history_count
        })

    def log_gemini_request(self, model: str, endpoint: str, payload_info: dict):
        """Log outgoing Gemini API request."""
        self.log("GEMINI_REQUEST", {
            "model": model,
            "endpoint": endpoint,
            "payload": payload_info
        })

    def log_gemini_response(self, model: str, response: dict, duration_ms: float):
        """Log incoming Gemini API response."""
        self.log("GEMINI_RESPONSE", {
            "model": model,
            "duration_ms": round(duration_ms, 2),
            "response": self._truncate_response(response)
        })

    def log_mcp_tool_call(self, tool_name: str, arguments: dict):
        """Log MCP tool call request."""
        self.log("MCP_TOOL_REQUEST", {
            "tool_name": tool_name,
            "arguments": arguments
        })

    def log_mcp_tool_result(self, tool_name: str, result: Any, duration_ms: float, status: str = "success"):
        """Log MCP tool call result."""
        result_str = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
        self.log("MCP_TOOL_RESPONSE", {
            "tool_name": tool_name,
            "duration_ms": round(duration_ms, 2),
            "status": status,
            "result": result_str  # No truncation - full result for debugging
        })

    def log_kb_query(self, message: str, result: str, duration_ms: float):
        """Log Knowledge Base query."""
        self.log("KB_QUERY", {
            "query": message,
            "duration_ms": round(duration_ms, 2),
            "result_length": len(result),
            "result": result  # No truncation - full result for debugging
        })

    def log_synthesis_start(self, context_parts: list):
        """Log synthesis phase start."""
        self.log("SYNTHESIS_START", {
            "context_sources": context_parts
        })

    def log_final_response(self, text: str, chart_config: dict = None, total_duration_ms: float = None):
        """Log the final response sent to user."""
        self.log("FINAL_RESPONSE", {
            "text_length": len(text),
            "text_preview": text[:500] + "..." if len(text) > 500 else text,
            "chart_config": chart_config,
            "total_duration_ms": round(total_duration_ms, 2) if total_duration_ms else None
        })

    def log_error(self, error_type: str, error_message: str, context: dict = None):
        """Log an error."""
        self.log("ERROR", {
            "error_type": error_type,
            "error_message": str(error_message),
            "context": context or {}
        })

    def _truncate_response(self, response: dict) -> dict:
        """Return full response for logging (no truncation)."""
        return response


# Flask app
app = Flask(__name__)
CORS(app)

# Global state
session_id = None
tools_cache = None
_tools_cache_time = 0.0
_TOOLS_CACHE_TTL = 300.0  # Re-fetch tools every 5 minutes (detects MCP server restarts)


def mcp_request(method: str, params: dict = None, is_notification: bool = False) -> dict:
    """Send a JSON-RPC request or notification to the MCP server.

    Args:
        method: The JSON-RPC method name
        params: Optional parameters
        is_notification: If True, sends as notification (no id, no response expected)
    """
    global session_id  # Needed to SET the global session_id from response headers

    payload = {
        "jsonrpc": "2.0",
        "method": method
    }

    # Notifications don't have an id
    if not is_notification:
        payload["id"] = int(time.time() * 1000)

    if params:
        payload["params"] = params

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }

    if session_id:
        headers["Mcp-Session-Id"] = session_id

    try:
        # For notifications, we send but don't expect a response
        if is_notification:
            requests.post(
                MCP_URL,
                json=payload,
                headers=headers,
                timeout=5
            )
            return {"result": "notification sent"}

        response = requests.post(
            MCP_URL,
            json=payload,
            headers=headers,
            timeout=300,
            stream=True
        )

        # Log response details for debugging
        logger.info(f"MCP Response - Status: {response.status_code}, Headers: {dict(response.headers)}")

        # Get session ID from response (try multiple header variations)
        session_header = (
            response.headers.get("Mcp-Session-Id") or
            response.headers.get("mcp-session-id") or
            response.headers.get("MCP-Session-ID")
        )
        if session_header:
            session_id = session_header
            logger.info(f"Got MCP session ID from headers: {session_id}")
        else:
            logger.warning(f"No session ID in response headers. Available headers: {list(response.headers.keys())}")

        content_type = response.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            # Parse SSE response
            result = None
            for line in response.iter_lines():
                if line:
                    line_str = line.decode('utf-8')
                    if line_str.startswith("data: "):
                        try:
                            data = json.loads(line_str[6:])
                            if "result" in data:
                                result = data["result"]
                            elif "error" in data:
                                return {"error": data["error"]}
                        except json.JSONDecodeError:
                            continue
            return {"result": result} if result else {"error": "No result"}
        else:
            return response.json()

    except requests.exceptions.ConnectionError:
        return {"error": f"Cannot connect to MCP server at {MCP_URL}. Make sure it's running!"}
    except Exception as e:
        return {"error": str(e)}


def initialize_mcp() -> bool:
    """Initialize the MCP session."""
    global session_id

    logger.info("Initializing MCP session...")

    result = mcp_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {"roots": {"listChanged": True}},
        "clientInfo": {"name": "dc-mcp-proxy", "version": "1.0.0"}
    })

    if "error" in result:
        logger.error(f"Failed to initialize MCP: {result['error']}")
        return False

    logger.info(f"MCP session initialized: {session_id}")

    # Send initialized notification (no id, no response expected)
    mcp_request("notifications/initialized", {}, is_notification=True)
    return True


def get_tools() -> list:
    """Get available tools from MCP server.

    Caches the tool list for _TOOLS_CACHE_TTL seconds so that MCP server
    restarts are detected within that window without fetching on every request.
    """
    global tools_cache, _tools_cache_time

    now = time.time()
    if tools_cache and (now - _tools_cache_time) < _TOOLS_CACHE_TTL:
        return tools_cache

    result = mcp_request("tools/list", {})

    if "result" in result and result["result"] and "tools" in result["result"]:
        tools_cache = result["result"]["tools"]
        _tools_cache_time = now
        return tools_cache

    return []


def transform_schema_for_gemini(schema: dict) -> dict:
    """Transform MCP inputSchema to Gemini-compatible format.

    Gemini function calling only supports a subset of OpenAPI 3.0.3 schema.
    This removes unsupported constructs like 'anyOf' for nullable types.

    Args:
        schema: The MCP inputSchema dictionary

    Returns:
        dict: Gemini-compatible schema
    """
    if not isinstance(schema, dict):
        return schema

    result = {}

    # Handle anyOf (union types) - common for nullable fields in MCP schemas
    # e.g., {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}
    if "anyOf" in schema:
        # Find the non-null type and use that
        for option in schema["anyOf"]:
            if option.get("type") != "null":
                result = transform_schema_for_gemini(option)
                break
        # Preserve default if present at the anyOf level
        if "default" in schema:
            result["default"] = schema["default"]
        # Preserve description if present at the anyOf level
        if "description" in schema:
            result["description"] = schema["description"]
        return result

    # Copy supported fields
    for key in ["type", "description", "default", "enum"]:
        if key in schema:
            result[key] = schema[key]

    # Handle object properties recursively
    if "properties" in schema:
        result["properties"] = {
            k: transform_schema_for_gemini(v)
            for k, v in schema["properties"].items()
        }

    # Handle required array
    if "required" in schema:
        result["required"] = schema["required"]

    # Handle array items recursively
    if "items" in schema:
        result["items"] = transform_schema_for_gemini(schema["items"])

    return result


def fix_tool_arguments(name: str, arguments: dict) -> dict:
    """Fix common parameter mistakes made by LLMs."""
    args = arguments.copy()

    if name == "get_observations":
        # Fix 1: If date_range_start/end provided but date != 'range', fix it
        has_range_params = args.get("date_range_start") or args.get("date_range_end")
        if has_range_params and args.get("date") != "range":
            logger.info("Fixing: Setting date='range' because date_range params provided")
            args["date"] = "range"

        # Fix 2: Ensure date has a default if not provided
        if "date" not in args:
            args["date"] = "latest"

        # Fix 3: Remove null/None values that might cause issues
        args = {k: v for k, v in args.items() if v is not None}

    if name == "search_indicators":
        # Fix: Ensure places is a list
        if "places" in args and isinstance(args["places"], str):
            args["places"] = [args["places"]]

    return args


def call_tool(name: str, arguments: dict, session_logger: Optional[SessionLogger] = None) -> Any:
    """Call a tool on the MCP server with optional logging."""
    # Fix common parameter mistakes
    fixed_args = fix_tool_arguments(name, arguments)
    if fixed_args != arguments:
        logger.info(f"Fixed arguments: {arguments} -> {fixed_args}")

    # Log tool call request
    if session_logger:
        session_logger.log_mcp_tool_call(name, fixed_args)

    start_time = time.time()

    result = mcp_request("tools/call", {
        "name": name,
        "arguments": fixed_args
    })

    duration_ms = (time.time() - start_time) * 1000

    if "result" in result:
        # Log successful result
        if session_logger:
            session_logger.log_mcp_tool_result(name, result["result"], duration_ms, "success")
        return result["result"]

    # Log error result
    error_result = {"error": result.get("error", "Unknown error")}
    if session_logger:
        session_logger.log_mcp_tool_result(name, error_result, duration_ms, "error")
    return error_result


# Pre-compiled patterns for check_data_availability — avoids re-compiling on every call
_RE_TIME_SERIES_HAS_DATA = re.compile(r'"time_series":\s*\[\s*\[')
_RE_VALID_SOURCE_ID = re.compile(r'"source_id":\s*"(?!unknown)[^"]+')


def check_data_availability(tool_calls_list: list) -> dict:
    """Check if MCP tool calls returned useful data.

    Returns:
        dict with keys:
        - has_data: bool
        - no_variables_found: bool (search_indicators returned empty)
        - no_observations_found: bool (get_observations returned empty)
        - message: str (user-friendly message if no data)
    """
    no_variables = False
    has_any_observations = False  # Track if ANY observation has data
    all_observations_empty = True  # Track if ALL observations are empty
    search_called = False
    observations_called = False

    for tc in tool_calls_list:
        result_str = tc.get('result', '')
        tool_name = tc.get('name', '')

        if tool_name == 'search_indicators':
            search_called = True
            # Check if no variables found (single lower() call)
            result_str_lower = result_str.lower()
            if 'no indicators found' in result_str_lower or \
               '"variables": []' in result_str_lower or \
               'no matching' in result_str_lower or \
               'could not find' in result_str_lower or \
               ('"indicators":' in result_str_lower and '[]' in result_str_lower):
                no_variables = True

        elif tool_name == 'get_observations':
            observations_called = True

            # Check if THIS observation has actual data (time_series with values)
            # Look for patterns like: "time_series": [["2024", 14984.0]] (has data)
            # vs: "time_series": [] (empty)

            # Check for non-empty time_series with actual values (pre-compiled)
            has_data_pattern = _RE_TIME_SERIES_HAS_DATA.search(result_str)
            if has_data_pattern:
                has_any_observations = True
                all_observations_empty = False

            # Also check for valid source_id (not "unknown")
            valid_source = _RE_VALID_SOURCE_ID.search(result_str.lower())
            if valid_source and has_data_pattern:
                has_any_observations = True
                all_observations_empty = False

            # Check if this specific observation is empty (single lower() call)
            result_str_lower = result_str.lower()
            is_empty = ('no data' in result_str_lower or
                       '"observations": []' in result_str_lower or
                       '"time_series": []' in result_str_lower or
                       '"time_series":[]' in result_str_lower or
                       'no observations' in result_str_lower)

            if not is_empty:
                all_observations_empty = False

    # Determine if we have usable data
    # We have data if: we found variables AND at least one observation has data
    if search_called and no_variables:
        has_data = False
    elif observations_called and all_observations_empty and not has_any_observations:
        has_data = False
    else:
        has_data = has_any_observations or (observations_called and not all_observations_empty)

    # Build user-friendly message
    message = None
    if not has_data:
        if no_variables:
            message = "We didn't find any matching data variables for your query."
        elif observations_called and all_observations_empty:
            message = "We found the data variable but there are no observations available."
        else:
            message = "We didn't find data for your query."

    return {
        'has_data': has_data,
        'no_variables_found': no_variables,
        'no_observations_found': all_observations_empty,
        'search_called': search_called,
        'observations_called': observations_called,
        'message': message
    }


def extract_provenance_from_mcp_results(tool_calls_list: list) -> list:
    """Extract provenance URLs from MCP tool call results.

    Parses the source_metadata from get_observations results to extract
    import_name and provenance_url for proper source attribution.
    Maps external URLs to NDAP-appropriate source URLs.

    Args:
        tool_calls_list: List of tool call dicts with 'name', 'arguments', 'result'

    Returns:
        list of dicts: [{"name": "Import Name", "url": "https://..."}]
    """
    # Map external provenance URLs to NDAP-appropriate sources
    PROVENANCE_URL_MAP = {
        "datacommons.org": ("NDAP Data Commons", "https://ndap.niti.gov.in/"),
        "ncvbdc.mohfw.gov.in": ("NCVBDC, MoHFW", "https://ndap.niti.gov.in/"),
        "censusindia.gov.in": ("Census of India", "https://ndap.niti.gov.in/"),
        "mospi.gov.in": ("MOSPI", "https://esankhyiki.mospi.gov.in/"),
        "esankhyiki.mospi.gov.in": ("MOSPI", "https://esankhyiki.mospi.gov.in/"),
        "tradestat.commerce.gov.in": ("DGCIS", "https://tradestat.commerce.gov.in/"),
    }

    def _map_url(url: str, name: str) -> tuple:
        """Map external provenance URL to NDAP source. Returns (name, url)."""
        for domain, (mapped_name, mapped_url) in PROVENANCE_URL_MAP.items():
            if domain in url:
                return (name or mapped_name, mapped_url)
        # For any other external URL, redirect to NDAP
        if url and "ndap.niti.gov.in" not in url:
            return (name or "NDAP Data Source", "https://ndap.niti.gov.in/")
        return (name or "Data Source", url)

    sources = []
    seen_urls = set()

    for tc in tool_calls_list:
        if tc.get('name') != 'get_observations':
            continue

        result_str = tc.get('result', '')
        try:
            # The result is nested JSON - parse outer layer first
            if isinstance(result_str, str):
                outer = json.loads(result_str)
                if 'content' in outer and outer['content']:
                    # Parse inner text JSON
                    inner_text = outer['content'][0].get('text', '{}')
                    result_data = json.loads(inner_text)
                else:
                    result_data = outer
            else:
                result_data = result_str

            # Extract from source_metadata
            if 'source_metadata' in result_data:
                metadata = result_data['source_metadata']
                url = metadata.get('provenance_url', '')
                name = metadata.get('import_name', '')

                mapped_name, mapped_url = _map_url(url, name)

                if mapped_url and mapped_url not in seen_urls:
                    seen_urls.add(mapped_url)
                    sources.append({
                        "name": mapped_name,
                        "url": mapped_url
                    })

        except (json.JSONDecodeError, KeyError, TypeError, IndexError):
            continue

    return sources


# ─── Response Cache ───────────────────────────────────────────────────────────
# In-memory cache keyed on normalized query text (first message only, no history).
# Stores the full list of SSE event strings so repeat queries replay instantly.
# TTL = 1 hour.  Max entries = 200 (LRU eviction).

import hashlib
from collections import OrderedDict

class ResponseCache:
    """Thread-safe LRU cache for SSE response sequences."""

    def __init__(self, max_size: int = 200, ttl_seconds: int = 3600):
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._ttl = ttl_seconds

    @staticmethod
    def _make_key(query: str) -> str:
        normalized = query.strip().lower()
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def get(self, query: str) -> list | None:
        key = self._make_key(query)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            ts, events = entry
            if time.time() - ts > self._ttl:
                del self._cache[key]
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            return events

    def put(self, query: str, events: list):
        key = self._make_key(query)
        with self._lock:
            self._cache[key] = (time.time(), events)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def size(self) -> int:
        return len(self._cache)

_response_cache = ResponseCache()


# Flask Routes

@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    return jsonify({"status": "ok", "mcp_url": MCP_URL, "cache_size": _response_cache.size()})


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    """Clear the response cache."""
    secret_key = request.args.get("key", "")
    if secret_key != get_query_param_key():
        return jsonify({"error": "Invalid key"}), 403
    old_size = _response_cache.size()
    _response_cache._cache.clear()
    return jsonify({"cleared": old_size, "cache_size": 0})


@app.route("/api/tools", methods=["GET"])
def list_tools():
    """List available tools."""
    global session_id

    if not session_id:
        if not initialize_mcp():
            return jsonify({"success": False, "error": "Cannot connect to MCP server. Make sure it's running on port 3000!"}), 503

    tools = get_tools()
    if not tools:
        return jsonify({"success": False, "error": "No tools available"}), 503

    # Convert to Gemini format (transform schema to remove unsupported constructs)
    gemini_tools = [{
        "name": t.get("name", ""),
        "description": t.get("description", ""),
        "parameters": transform_schema_for_gemini(
            t.get("inputSchema", {"type": "object", "properties": {}})
        )
    } for t in tools]

    return jsonify({"success": True, "tools": gemini_tools, "raw_tools": tools})


@app.route("/api/call", methods=["POST"])
def tool_call():
    """Execute a tool call."""
    global session_id

    if not session_id:
        if not initialize_mcp():
            return jsonify({"success": False, "error": "Cannot connect to MCP server"}), 503

    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"success": False, "error": "Tool name required"}), 400

    logger.info(f"Calling tool: {data['name']}")
    result = call_tool(data["name"], data.get("arguments", {}))
    return jsonify({"success": True, "result": result})


@app.route("/", methods=["GET"])
def index():
    return f"""
    <html>
    <head><title>MCP Proxy</title></head>
    <body>
    <h1>Data Commons MCP Proxy Server (Proxy-Only Mode)</h1>
    <p>MCP Server: {MCP_URL}</p>
    <p>Proxy Server: http://localhost:{PROXY_PORT}</p>
    <ul>
        <li><a href="/health">/health</a> - Health check</li>
        <li><a href="/api/tools">/api/tools</a> - List tools</li>
        <li>POST /api/call - Execute tool</li>
        <li><a href="/api/config">/api/config</a> - Get backend config (no API key)</li>
        <li>POST /api/chat/stream - Full chat with streaming</li>
        <li><a href="/logs?key=">/logs</a> - Query Analytics Dashboard (requires ?key=SECRET)</li>
    </ul>
    <h3>Prerequisite</h3>
    <p>Make sure the MCP server is running:</p>
    <code>python3 -m uv tool run datacommons-mcp serve http --port {MCP_PORT}</code>
    </body>
    </html>
    """


# ============================================================
# NEW BACKEND API ENDPOINTS FOR GEMINI CALLS
# ============================================================

@app.route("/api/config", methods=["GET"])
def get_config_endpoint():
    """Return sanitized config (without API key) for frontend."""
    config = load_config()
    if not config:
        return jsonify({"success": False, "error": "Config not loaded"}), 500

    # Return config without sensitive data or model names
    safe_config = {
        "proxy_url": config.get("proxy_url", f"http://localhost:{PROXY_PORT}"),
        "mcp": config.get("mcp", {}),
        "knowledge_base": config.get("knowledge_base", {}),
        "thinking": config.get("thinking", {}),
        "has_api_key": bool(config.get("gemini", {}).get("api_keys") or config.get("gemini", {}).get("api_key")),
    }
    return jsonify({"success": True, "config": safe_config})


def build_thinking_config(thinking_value: str, include_thoughts: bool = False) -> dict:
    """Build thinking configuration for Gemini 3 models.

    Args:
        thinking_value: Thinking level ('minimal', 'low', 'medium', 'high')
        include_thoughts: If True, includes thought summaries in the response

    Returns:
        dict: thinkingConfig for Gemini generationConfig
    """
    # Gemini 3 Flash valid levels
    valid_levels = ["minimal", "low", "medium", "high"]
    level = thinking_value.lower() if thinking_value.lower() in valid_levels else "low"

    config = {
        "thinkingConfig": {
            "thinkingLevel": level  # Gemini 3 format (string)
        }
    }

    if include_thoughts:
        config["thinkingConfig"]["includeThoughts"] = True

    return config


def gemini_request(
    messages: list,
    system_instruction: str,
    model: str,
    tools: list = None,
    temperature: float = 0,
    thinking_level: str = None,
    response_schema: dict = None,
    stream: bool = False,
    session_logger: Optional[SessionLogger] = None,
    include_thoughts: bool = False,
    demo_mode: bool = False
) -> Generator | dict:
    """Make a request to the Gemini API with key rotation and retry.

    Args:
        messages: Conversation history in Gemini format
        system_instruction: System prompt
        model: Model name (e.g., 'gemini-3-flash-preview')
        tools: Optional list of function declarations
        temperature: Sampling temperature
        thinking_level: Optional thinking budget level
        response_schema: Optional JSON schema for structured output
        stream: If True, returns a generator for SSE streaming
        session_logger: Optional SessionLogger for comprehensive logging
        include_thoughts: If True (and stream=True), yields dicts with 'type' and 'content'
                         for both thoughts and text. If False, yields plain text strings.
        demo_mode: If True, uses demo API keys reserved for internal demos.

    Returns:
        If stream=False: dict with response
        If stream=True and include_thoughts=False: Generator yielding text chunks (str)
        If stream=True and include_thoughts=True: Generator yielding dicts {'type': 'thought'|'text', 'content': str}
    """
    config = load_config()
    api_base = config.get("gemini", {}).get("api_base", "https://generativelanguage.googleapis.com/v1beta/models")

    # Get all available keys (demo or regular based on mode)
    all_keys = get_api_keys(demo_mode=demo_mode)
    if not all_keys:
        return {"error": "No Gemini API keys configured in config.json"}

    # Shuffle keys for random order
    keys_to_try = all_keys.copy()
    random.shuffle(keys_to_try)

    # Build the payload (same for all attempts)
    payload = {
        "contents": messages,
        "generationConfig": {
            "temperature": temperature,
        }
    }

    if system_instruction:
        payload["systemInstruction"] = {
            "parts": [{"text": inject_datetime(system_instruction)}]
        }

    if tools:
        payload["tools"] = [{"functionDeclarations": tools}]

    if thinking_level:
        # Enable includeThoughts in API if caller wants thought streaming
        payload["generationConfig"].update(
            build_thinking_config(thinking_level, include_thoughts=(stream and include_thoughts))
        )

    if response_schema:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseSchema"] = response_schema

    endpoint = "streamGenerateContent" if stream else "generateContent"

    # Log request (once, before attempting)
    if session_logger:
        session_logger.log_gemini_request(model, endpoint, {
            "messages_count": len(messages),
            "has_tools": bool(tools),
            "tool_count": len(tools) if tools else 0,
            "temperature": temperature,
            "thinking_level": thinking_level,
            "has_response_schema": bool(response_schema),
            "stream": stream,
            "total_keys_available": len(all_keys)
        })

    last_error = None
    attempt_count = 0

    for api_key in keys_to_try:
        attempt_count += 1

        # Build URL with current key
        url = f"{api_base}/{model}:{endpoint}"
        if stream:
            url += f"?key={api_key}&alt=sse"
        else:
            url += f"?key={api_key}"

        # Log retry attempt (if not first attempt)
        if attempt_count > 1 and session_logger:
            session_logger.log("GEMINI_KEY_ROTATION", {
                "attempt": attempt_count,
                "total_keys": len(all_keys),
                "reason": str(last_error)
            })

        start_time = time.time()

        try:
            if stream:
                response = requests.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    stream=True,
                    timeout=300
                )
                # Check for rate limit before streaming
                if response.status_code == 429:
                    last_error = "Rate limited (429)"
                    logger.warning(f"API key rate limited, switching to next key...")
                    continue  # Immediately try next key
                if response.status_code in [500, 503]:
                    last_error = f"Server error ({response.status_code})"
                    logger.warning(f"Server error {response.status_code}, switching to next key...")
                    continue  # Try next key
                return _stream_gemini_response(response, session_logger, return_dicts=include_thoughts)
            else:
                response = requests.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=300
                )

                # Check for rate limit - immediately switch key
                if response.status_code == 429:
                    last_error = "Rate limited (429)"
                    logger.warning(f"API key rate limited, switching to next key...")
                    continue  # Immediately try next key

                # Check for other retryable errors (500, 503)
                if response.status_code in [500, 503]:
                    last_error = f"Server error ({response.status_code})"
                    logger.warning(f"Server error {response.status_code}, switching to next key...")
                    continue  # Try next key

                result = response.json()

                # Log response
                if session_logger:
                    duration_ms = (time.time() - start_time) * 1000
                    session_logger.log_gemini_response(model, result, duration_ms)

                return result

        except requests.exceptions.Timeout:
            last_error = "Request timeout"
            logger.warning(f"Request timeout, trying next key...")
            continue
        except Exception as e:
            last_error = str(e)
            logger.error(f"Gemini API error: {e}")
            if session_logger:
                session_logger.log_error("GEMINI_API_ERROR", str(e), {"attempt": attempt_count, "model": model})
            continue

    # All keys exhausted
    error_msg = f"All {len(all_keys)} API keys failed. Last error: {last_error}"
    logger.error(error_msg)
    if session_logger:
        session_logger.log_error("GEMINI_ALL_KEYS_EXHAUSTED", error_msg, {"total_keys": len(all_keys)})
    return {"error": error_msg}


def _stream_gemini_response(response, session_logger: Optional[SessionLogger] = None, return_dicts: bool = False) -> Generator:
    """Parse streaming response from Gemini API.

    Args:
        response: The requests response object with streaming enabled
        session_logger: Optional SessionLogger for logging
        return_dicts: If True, yields dicts with 'type' and 'content' keys
                      for both thoughts and text. If False, yields plain text strings.

    Yields:
        If return_dicts=True: {'type': 'thought'|'text', 'content': str}
        If return_dicts=False: str (text only, for backward compatibility)
    """
    start_time = time.time()
    total_text = ""
    total_thoughts = ""

    for line in response.iter_lines():
        if line:
            line_str = line.decode('utf-8')
            if line_str.startswith('data: '):
                try:
                    data = json.loads(line_str[6:])
                    if 'candidates' in data and data['candidates']:
                        candidate = data['candidates'][0]
                        if 'content' in candidate and 'parts' in candidate['content']:
                            for part in candidate['content']['parts']:
                                if 'text' in part:
                                    # Check if this is a thought summary or regular text
                                    is_thought = part.get('thought', False)
                                    if is_thought:
                                        total_thoughts += part['text']
                                        if return_dicts:
                                            yield {'type': 'thought', 'content': part['text']}
                                        # Skip thoughts in legacy mode (return_dicts=False)
                                    else:
                                        total_text += part['text']
                                        if return_dicts:
                                            yield {'type': 'text', 'content': part['text']}
                                        else:
                                            yield part['text']
                except json.JSONDecodeError:
                    continue

    # Log streaming completion
    if session_logger:
        duration_ms = (time.time() - start_time) * 1000
        session_logger.log("GEMINI_STREAM_COMPLETE", {
            "duration_ms": round(duration_ms, 2),
            "total_text_length": len(total_text),
            "total_thoughts_length": len(total_thoughts)
        })


def gemini_request_with_thought_streaming(
    messages: list,
    system_instruction: str,
    model: str,
    tools: list = None,
    temperature: float = 0,
    thinking_level: str = None,
    response_schema: dict = None,
    session_logger: Optional[SessionLogger] = None,
    thought_callback: callable = None,
    demo_mode: bool = False
) -> dict:
    """Make a streaming Gemini request, calling thought_callback for thoughts but returning complete response.

    This enables thought streaming for reduced TTFT while still getting the
    complete response needed for tool call processing.

    Args:
        messages: Conversation history in Gemini format
        system_instruction: System prompt
        model: Model name (e.g., 'gemini-3-flash-preview')
        tools: Optional list of function declarations
        temperature: Sampling temperature
        thinking_level: Optional thinking budget level
        response_schema: Optional JSON schema for structured output
        session_logger: Optional SessionLogger for comprehensive logging
        thought_callback: Optional callback function called with each thought chunk.
                         Signature: callback(thought_text: str) -> None
        demo_mode: If True, uses demo API keys reserved for internal demos.

    Returns:
        dict: Complete response (same format as non-streaming gemini_request)
    """
    config = load_config()
    api_base = config.get("gemini", {}).get("api_base", "https://generativelanguage.googleapis.com/v1beta/models")

    # Get all available keys (demo or regular based on mode)
    all_keys = get_api_keys(demo_mode=demo_mode)
    if not all_keys:
        return {"error": "No Gemini API keys configured in config.json"}

    # Shuffle keys for random order
    keys_to_try = all_keys.copy()
    random.shuffle(keys_to_try)

    # Build payload
    payload = {
        "contents": messages,
        "generationConfig": {
            "temperature": temperature,
        }
    }

    if system_instruction:
        payload["systemInstruction"] = {
            "parts": [{"text": inject_datetime(system_instruction)}]
        }

    if tools:
        payload["tools"] = [{"functionDeclarations": tools}]

    if thinking_level:
        # Enable includeThoughts for streaming thought summaries
        payload["generationConfig"].update(
            build_thinking_config(thinking_level, include_thoughts=True)
        )

    if response_schema:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseSchema"] = response_schema

    # Log request
    if session_logger:
        session_logger.log_gemini_request(model, "streamGenerateContent", {
            "messages_count": len(messages),
            "has_tools": bool(tools),
            "tool_count": len(tools) if tools else 0,
            "temperature": temperature,
            "thinking_level": thinking_level,
            "include_thoughts": True,
            "total_keys_available": len(all_keys)
        })

    last_error = None
    attempt_count = 0

    for api_key in keys_to_try:
        attempt_count += 1

        # Build URL for streaming
        url = f"{api_base}/{model}:streamGenerateContent?key={api_key}&alt=sse"

        # Log retry attempt (if not first attempt)
        if attempt_count > 1 and session_logger:
            session_logger.log("GEMINI_KEY_ROTATION", {
                "attempt": attempt_count,
                "total_keys": len(all_keys),
                "reason": str(last_error)
            })

        start_time = time.time()

        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                stream=True,
                timeout=300
            )

            # Check for rate limit before streaming
            if response.status_code == 429:
                last_error = "Rate limited (429)"
                logger.warning(f"API key rate limited, switching to next key...")
                continue
            if response.status_code in [500, 503]:
                last_error = f"Server error ({response.status_code})"
                logger.warning(f"Server error {response.status_code}, switching to next key...")
                continue

            # Collect response while streaming thoughts
            collected_text = ""
            collected_function_calls = []
            collected_thoughts = ""

            for line in response.iter_lines():
                if line:
                    line_str = line.decode('utf-8')
                    if line_str.startswith('data: '):
                        try:
                            data = json.loads(line_str[6:])
                            if 'candidates' in data and data['candidates']:
                                candidate = data['candidates'][0]
                                if 'content' in candidate and 'parts' in candidate['content']:
                                    for part in candidate['content']['parts']:
                                        if 'functionCall' in part:
                                            collected_function_calls.append(part)
                                        elif 'text' in part:
                                            is_thought = part.get('thought', False)
                                            if is_thought:
                                                collected_thoughts += part['text']
                                                if thought_callback:
                                                    thought_callback(part['text'])
                                            else:
                                                collected_text += part['text']
                        except json.JSONDecodeError:
                            continue

            # Build response in same format as non-streaming
            result_parts = []
            for fc in collected_function_calls:
                result_parts.append(fc)
            if collected_text:
                result_parts.append({"text": collected_text})

            result = {
                "candidates": [{
                    "content": {
                        "parts": result_parts,
                        "role": "model"
                    }
                }]
            }

            # Log response
            if session_logger:
                duration_ms = (time.time() - start_time) * 1000
                session_logger.log_gemini_response(model, result, duration_ms)
                if collected_thoughts:
                    session_logger.log("THOUGHTS_STREAMED", {
                        "thoughts_length": len(collected_thoughts)
                    })

            return result

        except requests.exceptions.Timeout:
            last_error = "Request timeout"
            logger.warning(f"Request timeout, trying next key...")
            continue
        except Exception as e:
            last_error = str(e)
            logger.error(f"Gemini API error: {e}")
            if session_logger:
                session_logger.log_error("GEMINI_API_ERROR", str(e), {"attempt": attempt_count, "model": model})
            continue

    # All keys exhausted
    error_msg = f"All {len(all_keys)} API keys failed. Last error: {last_error}"
    logger.error(error_msg)
    if session_logger:
        session_logger.log_error("GEMINI_ALL_KEYS_EXHAUSTED", error_msg, {"total_keys": len(all_keys)})
    return {"error": error_msg}


def execute_mcp_tool_loop(
    user_message: str,
    history: list,
    max_iterations: int = 5,
    session_logger: Optional[SessionLogger] = None,
    effective_config: dict = None,
    thought_callback: callable = None,
    demo_mode: bool = False
) -> tuple:
    """Execute the MCP tool calling loop with optional thought streaming.

    Args:
        user_message: The user's query
        history: Conversation history
        max_iterations: Maximum tool calling iterations
        session_logger: Optional SessionLogger for comprehensive logging
        effective_config: Optional config dict with query param overrides applied
        thought_callback: Optional callback for streaming thought chunks.
                         Signature: callback(thought_text: str) -> None
        demo_mode: If True, uses demo API keys reserved for internal demos.

    Returns:
        tuple: (tool_results_text, tool_calls_list, final_response_text)
    """
    config = effective_config if effective_config else load_config()
    mcp_prompt = config.get("prompts", {}).get("mcp", "")
    thinking_level = config.get("thinking", {}).get("mcp_level", "low")

    # Determine MCP provider: "gemma4" uses OpenRouter, anything else uses Gemini
    mcp_provider = config.get("mcp", {}).get("provider", "gemma4")
    if mcp_provider == "gemma4":
        g4_config = config.get("gemma4", {})
        mcp_model = g4_config.get("mcp_model", "") or g4_config.get("synthesis_model", "google/gemma-4-26b-a4b-it")
        use_openrouter_mcp = True
        if session_logger:
            session_logger.log("MCP_PROVIDER", {"provider": "gemma4_openrouter", "model": mcp_model})
    else:
        mcp_model = config.get("gemini", {}).get("mcp_model", "gemini-3-flash-preview")
        use_openrouter_mcp = False
        if session_logger:
            session_logger.log("MCP_PROVIDER", {"provider": "gemini", "model": mcp_model})

    # Get MCP tools
    tools = get_tools()
    if not tools:
        if session_logger:
            session_logger.log_error("MCP_TOOLS_UNAVAILABLE", "No MCP tools available")
        return "", [], "MCP tools not available"

    # Convert tools to Gemini format (transform schema to remove unsupported constructs)
    gemini_tools = [{
        "name": t.get("name", ""),
        "description": t.get("description", ""),
        "parameters": transform_schema_for_gemini(
            t.get("inputSchema", {"type": "object", "properties": {}})
        )
    } for t in tools]

    # Build conversation context from history for cross-turn awareness
    # Extract a compact summary of previous data availability from history
    history_context = ""
    if history:
        prev_data_mentions = []
        for msg in history[-6:]:  # Last 3 turns (user+model pairs)
            parts = msg.get("parts", [])
            for p in parts:
                text = p.get("text", "")
                if text and msg.get("role") == "model" and len(text) > 50:
                    # Extract key data mentions from previous model responses
                    for keyword in ["NDAP_", "Count_Person", "not available", "no data"]:
                        if keyword.lower() in text.lower():
                            # Grab a short snippet around the keyword
                            idx = text.lower().find(keyword.lower())
                            snippet = text[max(0, idx-30):idx+80].strip()
                            if snippet and snippet not in prev_data_mentions:
                                prev_data_mentions.append(snippet)

        if prev_data_mentions:
            history_context = (
                "\n\n[CONVERSATION CONTEXT — Previous turns found these data points. "
                "Avoid re-querying variables that already returned data or were confirmed empty:]\n"
                + "\n".join(f"- {s}" for s in prev_data_mentions[:10])
            )

    contents = []
    contents.append({"role": "user", "parts": [{"text": user_message + history_context}]})

    tool_calls_list = []
    all_tool_results = []
    search_empty_count = 0  # S5: track consecutive empty search_indicators results
    MAX_EMPTY_SEARCHES = 2  # Cap search_indicators retries

    for iteration in range(max_iterations):
        logger.info(f"MCP Tool Loop - Iteration {iteration + 1}/{max_iterations}")

        if session_logger:
            session_logger.log("MCP_LOOP_ITERATION", {"iteration": iteration + 1, "max": max_iterations})

        if use_openrouter_mcp:
            response = openrouter_mcp_request(
                messages=contents,
                system_instruction=mcp_prompt,
                model=mcp_model,
                tools=gemini_tools,
                temperature=0,
                thinking_level=thinking_level,
                session_logger=session_logger,
                thought_callback=thought_callback,
                demo_mode=demo_mode
            )
        else:
            response = gemini_request_with_thought_streaming(
                messages=contents,
                system_instruction=mcp_prompt,
                model=mcp_model,
                tools=gemini_tools,
                temperature=0,
                thinking_level=thinking_level,
                session_logger=session_logger,
                thought_callback=thought_callback,
                demo_mode=demo_mode
            )

        if "error" in response:
            if session_logger:
                session_logger.log_error("MCP_LOOP_ERROR", response['error'])
            return "", tool_calls_list, f"Error: {response['error']}", iteration + 1

        # Check for function calls
        candidates = response.get("candidates", [])
        if not candidates:
            if session_logger:
                session_logger.log_error("MCP_NO_CANDIDATES", "No response from model")
            return "", tool_calls_list, "No response from model", iteration + 1

        candidate = candidates[0]
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        function_calls = []
        text_response = ""

        for part in parts:
            if "functionCall" in part:
                function_calls.append(part["functionCall"])
            elif "text" in part:
                text_response += part["text"]

        # If no function calls, we're done
        if not function_calls:
            tool_results_text = "\n\n".join(all_tool_results)
            if session_logger:
                session_logger.log("MCP_LOOP_COMPLETE", {
                    "iterations_used": iteration + 1,
                    "tools_called": len(tool_calls_list),
                    "has_text_response": bool(text_response)
                })
            return tool_results_text, tool_calls_list, text_response, iteration + 1

        # Execute function calls — run independent calls in parallel
        contents.append({"role": "model", "parts": parts})
        function_responses = []

        def _run_tool(fc: dict):
            """Execute a single tool call and return (fc, result_text)."""
            t_name = fc.get("name", "")
            t_args = fc.get("args", {})
            logger.info(f"Executing MCP tool: {t_name}")
            res = call_tool(t_name, t_args, session_logger=session_logger)
            if isinstance(res, dict):
                if "content" in res and isinstance(res["content"], list):
                    r_text = "\n".join([c.get("text", json.dumps(c)) for c in res["content"]])
                else:
                    r_text = json.dumps(res)
            else:
                r_text = str(res)
            return fc, r_text

        if len(function_calls) == 1:
            # Skip thread overhead for the common single-tool case
            results_ordered = [_run_tool(function_calls[0])]
        else:
            # Dispatch all tool calls concurrently; preserve submission order in output
            with ThreadPoolExecutor(max_workers=min(len(function_calls), 5)) as pool:
                futures = {pool.submit(_run_tool, fc): i for i, fc in enumerate(function_calls)}
                results_ordered = [None] * len(function_calls)
                for future in as_completed(futures):
                    idx = futures[future]
                    results_ordered[idx] = future.result()

        for fc, result_text in results_ordered:
            tool_name = fc.get("name", "")
            tool_args = fc.get("args", {})

            tool_call_info = {
                "name": tool_name,
                "arguments": tool_args,
                "result": result_text,  # No truncation - full result for source extraction
                "status": "error" if "error" in result_text.lower() else "success"
            }
            tool_calls_list.append(tool_call_info)
            all_tool_results.append(f"Tool: {tool_name}\nResult: {result_text}")

            # S5: Track empty search_indicators results
            if tool_name == "search_indicators" and '"variables": []' in result_text:
                search_empty_count += 1
                if session_logger:
                    session_logger.log("SEARCH_EMPTY", {"count": search_empty_count, "query": str(tool_args.get("query", ""))[:100]})

            function_responses.append({
                "functionResponse": {
                    "name": tool_name,
                    "response": {"result": result_text}
                }
            })

        # S5: If search_indicators returned empty too many times, inject guidance
        if search_empty_count >= MAX_EMPTY_SEARCHES:
            _guidance = (
                "[SYSTEM NOTE: search_indicators returned empty results multiple times. "
                "Stop calling search_indicators. Either use get_observations with known DCIDs "
                "from the variable reference in your instructions, or conclude that the data "
                "is not available in this instance.]"
            )
            function_responses.append({"text": _guidance})
            if session_logger:
                session_logger.log("SEARCH_CAP_REACHED", {"empty_searches": search_empty_count})

        contents.append({"role": "user", "parts": function_responses})

    # Max iterations reached
    tool_results_text = "\n\n".join(all_tool_results)
    if session_logger:
        session_logger.log("MCP_LOOP_MAX_ITERATIONS", {"tools_called": len(tool_calls_list)})
    return tool_results_text, tool_calls_list, "Max tool iterations reached", max_iterations


def get_api_key_filestore_mapping(demo_mode: bool = False) -> dict:
    """Build mapping of API key -> filestore from config.

    Each API key in gemini.api_keys maps to the filestore at the same index
    in gemini.filestores array.

    Args:
        demo_mode: If True, uses demo_api_keys and demo_filestores for mapping.

    Returns:
        dict mapping api_key -> filestore_id
    """
    config = load_config()
    gemini_config = config.get("gemini", {})

    if demo_mode:
        api_keys = gemini_config.get("demo_api_keys", [])
        filestores = gemini_config.get("demo_filestores", [])
        if api_keys:
            logger.info(f"Using demo filestore mapping ({len(api_keys)} keys)")
    else:
        api_keys = gemini_config.get("api_keys", [])
        filestores = gemini_config.get("filestores", [])

    # Build the mapping - each key maps to filestore at same index
    mapping = {}
    for i, key in enumerate(api_keys):
        if i < len(filestores):
            mapping[key] = filestores[i]
        else:
            # Fallback to legacy store_id if no filestore configured for this key
            legacy_store = config.get("knowledge_base", {}).get("store_id", "")
            mapping[key] = legacy_store

    return mapping


def execute_kb_query(user_message: str, session_logger: Optional[SessionLogger] = None, thought_callback: callable = None, demo_mode: bool = False, effective_config: dict = None) -> dict:
    """Execute Knowledge Base query using file search with key rotation and thought streaming.

    Each API key automatically uses its paired filestore from the config mapping.

    Args:
        user_message: The user's query
        session_logger: Optional SessionLogger for logging
        thought_callback: Optional callback for streaming thought chunks.
                         Signature: callback(thought_text: str) -> None
        demo_mode: If True, uses demo API keys and filestores reserved for internal demos.
        effective_config: Optional config dict with query param overrides applied.

    Returns:
        dict with keys:
        - response: str (the response text)
        - sources: list of dicts with 'title' and 'uri'
    """
    config = effective_config if effective_config else load_config()
    kb_config = config.get("knowledge_base", {})

    if not kb_config.get("enabled", False):
        return {"response": "", "sources": []}

    kb_prompt = config.get("prompts", {}).get("kb", "")
    kb_model = config.get("gemini", {}).get("kb_model", "gemini-3-flash-preview")

    # Get API key -> filestore mapping (demo or regular based on mode)
    key_filestore_map = get_api_key_filestore_mapping(demo_mode=demo_mode)

    if not key_filestore_map:
        logger.warning("No API key to filestore mapping configured")
        return {"response": "", "sources": []}

    # Get all available keys (demo or regular based on mode)
    all_keys = get_api_keys(demo_mode=demo_mode)
    if not all_keys:
        return {"response": "", "sources": []}

    api_base = config.get("gemini", {}).get("api_base", "https://generativelanguage.googleapis.com/v1beta/models")

    # Shuffle keys for random order
    keys_to_try = all_keys.copy()
    random.shuffle(keys_to_try)

    last_error = None
    attempt_count = 0

    for api_key in keys_to_try:
        # Get the filestore for this specific API key
        store_id = key_filestore_map.get(api_key, "")
        if not store_id:
            logger.warning(f"No filestore configured for API key, skipping...")
            continue

        logger.info(f"KB query using filestore: {store_id[:50]}...")

        # Build payload with this key's filestore and thinking config
        thinking_level = config.get("thinking", {}).get("kb_level", "low")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "systemInstruction": {"parts": [{"text": inject_datetime(kb_prompt)}]},
            "generationConfig": {
                "temperature": 0,
            },
            "tools": [{
                "fileSearch": {
                    "fileSearchStoreNames": [store_id]
                }
            }]
        }

        # Note: thinking config omitted for KB — file search uses stable gemini-2.5-flash
        # which does not support thinkingConfig in the same format

        attempt_count += 1
        start_time = time.time()

        # Log retry attempt (if not first attempt)
        if attempt_count > 1 and session_logger:
            session_logger.log("KB_KEY_ROTATION", {
                "attempt": attempt_count,
                "total_keys": len(all_keys),
                "reason": str(last_error)
            })

        try:
            # Use streaming endpoint to get thoughts in real-time
            url = f"{api_base}/{kb_model}:generateContent?key={api_key}"
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                stream=False,
                timeout=300
            )

            # Check for rate limit - immediately switch key
            logger.info(f"KB HTTP status: {response.status_code}")
            if response.status_code == 429:
                last_error = "Rate limited (429)"
                logger.warning(f"KB API key rate limited, switching to next key...")
                continue

            # Check for other retryable errors
            if response.status_code in [500, 503]:
                last_error = f"Server error ({response.status_code})"
                logger.warning(f"KB server error {response.status_code}, switching to next key...")
                continue

            # Collect response (non-streaming JSON for file search compatibility)
            result_text = ""
            sources = []
            collected_thoughts = ""
            grounding_metadata = {}

            try:
                data = response.json()
            except Exception as e:
                logger.error(f"KB JSON parse error: {e}")
                data = {}

            if 'candidates' in data and data['candidates']:
                candidate = data['candidates'][0]
                if 'groundingMetadata' in candidate:
                    grounding_metadata = candidate['groundingMetadata']
                if 'content' in candidate and 'parts' in candidate['content']:
                    for part in candidate['content']['parts']:
                        if 'text' in part:
                            is_thought = part.get('thought', False)
                            if is_thought:
                                collected_thoughts += part['text']
                                if thought_callback:
                                    thought_callback(part['text'])
                            else:
                                result_text += part['text']

            # Extract source citations from grounding metadata
            grounding_chunks = grounding_metadata.get("groundingChunks", [])
            seen_titles = set()
            for chunk in grounding_chunks:
                retrieved_context = chunk.get("retrievedContext", {})
                if retrieved_context:
                    title = retrieved_context.get("title", "Unknown")
                    uri = retrieved_context.get("uri", "")
                    # Deduplicate by title
                    if title not in seen_titles:
                        seen_titles.add(title)
                        sources.append({
                            "title": title,
                            "uri": uri
                        })

            # Log KB query
            if session_logger:
                duration_ms = (time.time() - start_time) * 1000
                session_logger.log_kb_query(user_message, result_text, duration_ms)
                if sources:
                    session_logger.log("KB_SOURCES", {"sources": sources})
                if collected_thoughts:
                    session_logger.log("KB_THOUGHTS_STREAMED", {"thoughts_length": len(collected_thoughts)})

            return {"response": result_text, "sources": sources}

        except requests.exceptions.Timeout:
            last_error = "Request timeout"
            logger.warning(f"KB request timeout, trying next key...")
            continue
        except Exception as e:
            last_error = str(e)
            logger.error(f"KB query error: {e}")
            if session_logger:
                session_logger.log_error("KB_QUERY_ERROR", str(e), {"query": user_message, "attempt": attempt_count})
            continue

    # All keys exhausted
    logger.error(f"KB query failed: All {len(all_keys)} API keys exhausted. Last error: {last_error}")
    if session_logger:
        session_logger.log_error("KB_ALL_KEYS_EXHAUSTED", f"All keys failed: {last_error}", {"total_keys": len(all_keys)})
    return {"response": "", "sources": []}


# Chart config schema for Gemini structured output (hardcoded - not user configurable)
# Supports multiple charts for variables with different units/scales
CHART_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "should_render": {
            "type": "boolean",
            "description": "True if at least one chart should be rendered"
        },
        "charts": {
            "type": "array",
            "description": "Array of chart configurations (max 3). Group compatible variables together.",
            "items": {
                "type": "object",
                "properties": {
                    "viz_type": {
                        "type": "string",
                        "enum": ["line", "bar", "ranking", "pie", "highlight", "gauge", "scatter", "slider","map"]
                    },
                    "title": {"type": "string", "description": "Descriptive chart title"},
                    "variable_dcids": {"type": "array", "items": {"type": "string"}},
                    "place_dcids": {"type": "array", "items": {"type": "string"}},
                    "parent_place": {"type": "string"},
                    "child_place_type": {"type": "string"},
                    "date": {
                        "type": "string",
                        "description": "ISO-8601 date (YYYY, YYYY-MM, or YYYY-MM-DD). Only for: highlight, map. Use when query mentions a specific year."
                    },
                    "startDate": {
                        "type": "string",
                        "description": "Earliest date (ISO-8601). Only for line charts. Use when query specifies a year or date range start."
                    },
                    "endDate": {
                        "type": "string",
                        "description": "Latest date (ISO-8601). Only for line charts. Use when query specifies a year or date range end."
                    },
                    "dates": {
                        "type": "string",
                        "description": "Space-separated years for slider charts (e.g. '2001 2002 2003')"
                    }
                }
            }
        }
    },
    "required": ["should_render"]
}




def openrouter_mcp_request(
    messages: list,
    system_instruction: str,
    model: str,
    tools: list = None,
    temperature: float = 0,
    thinking_level: str = None,
    response_schema: dict = None,
    session_logger: Optional[SessionLogger] = None,
    thought_callback: callable = None,
    demo_mode: bool = False
) -> dict:
    """Make an OpenRouter function-calling request, returning Gemini-format response.

    Drop-in replacement for gemini_request_with_thought_streaming so the MCP loop
    can run on Gemma 4 (or any OpenRouter model that supports tool use) without
    changing the loop logic.

    Converts:  Gemini tool schema  -> OpenAI tools format
               Gemini messages     -> OpenAI messages (incl. tool_calls / tool results)
               OpenAI response     -> Gemini response dict
    """
    config = load_config()
    g4_config = config.get("gemma4", {})
    or_config = config.get("openrouter", {})
    api_key = g4_config.get("api_key", "") or os.environ.get("OPENROUTER_API_KEY") or or_config.get("api_key", "")
    api_base = g4_config.get("api_base", "") or or_config.get("api_base", "https://openrouter.ai/api/v1")

    if not api_key or api_key == "PASTE_YOUR_OPENROUTER_API_KEY_HERE":
        return {"error": "OpenRouter API key not configured for MCP."}

    # --- Convert Gemini tools to OpenAI format ---
    oai_tools = None
    if tools:
        oai_tools = []
        for t in tools:
            oai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}})
                }
            })

    # --- Convert Gemini messages to OpenAI format ---
    oai_messages = []
    if system_instruction:
        oai_messages.append({"role": "system", "content": inject_datetime(system_instruction)})

    for msg in messages:
        role = msg.get("role", "user")
        parts = msg.get("parts", [])

        if role == "model":
            # Check if this model message contains tool calls
            tool_calls_oai = []
            text_content = ""
            for p in parts:
                if "functionCall" in p:
                    fc = p["functionCall"]
                    tool_calls_oai.append({
                        "id": f"call_{fc.get('name', 'unknown')}_{len(tool_calls_oai)}",
                        "type": "function",
                        "function": {
                            "name": fc.get("name", ""),
                            "arguments": json.dumps(fc.get("args", {}))
                        }
                    })
                elif "text" in p:
                    text_content += p["text"]
            assistant_msg = {"role": "assistant"}
            if tool_calls_oai:
                assistant_msg["tool_calls"] = tool_calls_oai
                assistant_msg["content"] = text_content or None
            else:
                assistant_msg["content"] = text_content
            oai_messages.append(assistant_msg)

        elif role == "user":
            # User parts may contain functionResponse items (tool results)
            func_responses = [p for p in parts if "functionResponse" in p]
            text_parts = [p.get("text", "") for p in parts if "text" in p and "functionResponse" not in p]

            if func_responses:
                for fr in func_responses:
                    fr_data = fr["functionResponse"]
                    oai_messages.append({
                        "role": "tool",
                        "tool_call_id": f"call_{fr_data.get('name', 'unknown')}_0",
                        "content": json.dumps(fr_data.get("response", {})) if isinstance(fr_data.get("response"), dict) else str(fr_data.get("response", ""))
                    })
                # Also add any text guidance (e.g. S5 search cap message) as a user message
                if text_parts:
                    combined = " ".join(t for t in text_parts if t.strip())
                    if combined.strip():
                        oai_messages.append({"role": "user", "content": combined})
            else:
                content = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
                oai_messages.append({"role": "user", "content": content})
        else:
            content = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
            oai_messages.append({"role": role, "content": content})

    # --- Fix tool_call_id references ---
    # OpenAI requires tool results to reference the exact tool_call_id from the assistant message.
    # Re-scan and fix IDs so they match.
    _last_tool_calls = {}
    for i, m in enumerate(oai_messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            _last_tool_calls = {tc["function"]["name"]: tc["id"] for tc in m["tool_calls"]}
        elif m.get("role") == "tool" and _last_tool_calls:
            # Extract the tool name from the placeholder ID
            placeholder_id = m.get("tool_call_id", "")
            for tname, tid in _last_tool_calls.items():
                if tname in placeholder_id:
                    m["tool_call_id"] = tid
                    break

    payload = {
        "model": model,
        "messages": oai_messages,
        "temperature": temperature,
    }
    if oai_tools:
        payload["tools"] = oai_tools
        payload["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://ndap.niti.gov.in",
        "X-Title": "NDAP Data Agent",
    }

    if session_logger:
        session_logger.log("OPENROUTER_MCP_REQUEST", {
            "model": model,
            "messages_count": len(oai_messages),
            "has_tools": bool(oai_tools),
            "tool_count": len(oai_tools) if oai_tools else 0
        })

    try:
        start_time = time.time()
        response = requests.post(
            f"{api_base}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120
        )

        duration_ms = (time.time() - start_time) * 1000
        if session_logger:
            session_logger.log("OPENROUTER_MCP_RESPONSE", {
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2)
            })

        if response.status_code != 200:
            error_text = response.text[:500]
            logger.error(f"OpenRouter MCP error {response.status_code}: {error_text}")
            return {"error": f"OpenRouter returned {response.status_code}: {error_text}"}

        data = response.json()
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        # --- Convert OpenAI response back to Gemini format ---
        gemini_parts = []
        oai_tool_calls = message.get("tool_calls", [])
        if oai_tool_calls:
            for tc in oai_tool_calls:
                func = tc.get("function", {})
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                gemini_parts.append({
                    "functionCall": {
                        "name": func.get("name", ""),
                        "args": args
                    }
                })
        text_content = message.get("content", "")
        if text_content:
            gemini_parts.append({"text": text_content})

        if not gemini_parts:
            gemini_parts.append({"text": message.get("content", "") or ""})

        return {
            "candidates": [{
                "content": {
                    "parts": gemini_parts,
                    "role": "model"
                },
                "finishReason": choice.get("finish_reason", "STOP")
            }]
        }

    except requests.exceptions.Timeout:
        logger.error("OpenRouter MCP request timed out")
        return {"error": "OpenRouter request timed out (120s)"}
    except Exception as e:
        logger.error(f"OpenRouter MCP error: {e}")
        return {"error": str(e)}


def openrouter_stream_request(
    messages: list,
    system_instruction: str,
    model: str,
    temperature: float = 0,
    session_logger=None
):
    """Stream a response from OpenRouter (OpenAI-compatible API).

    Yields dicts with 'type' and 'content' keys (same interface as gemini_request include_thoughts=True).
    """
    config = load_config()
    or_config = config.get("openrouter", {})
    # Prefer env var over config file so the key is never stored in plain text
    api_key = os.environ.get("OPENROUTER_API_KEY") or or_config.get("api_key", "")
    api_base = or_config.get("api_base", "https://openrouter.ai/api/v1")

    if not api_key or api_key == "PASTE_YOUR_OPENROUTER_API_KEY_HERE":
        logger.error("OpenRouter API key not configured. Set OPENROUTER_API_KEY env var.")
        yield {"type": "text", "content": "Error: OPENROUTER_API_KEY environment variable not set."}
        return

    # Convert Gemini-format messages to OpenAI format
    oai_messages = []
    if system_instruction:
        oai_messages.append({"role": "system", "content": system_instruction})
    for msg in messages:
        role = msg.get("role", "user")
        if role == "model":
            role = "assistant"
        parts = msg.get("parts", [])
        content = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
        oai_messages.append({"role": role, "content": content})

    payload = {
        "model": model,
        "messages": oai_messages,
        "temperature": temperature,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://ndap.niti.gov.in",
        "X-Title": "NDAP Data Agent",
    }

    try:
        if session_logger:
            session_logger.log("OPENROUTER_REQUEST", {"model": model, "messages_count": len(oai_messages)})

        response = requests.post(
            f"{api_base}/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=120
        )
        response.raise_for_status()

        for line in response.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8") if isinstance(line, bytes) else line
            if decoded.startswith("data: "):
                decoded = decoded[6:]
            if decoded.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(decoded)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield {"type": "text", "content": content}
            except json.JSONDecodeError:
                continue

    except Exception as e:
        logger.error(f"OpenRouter stream error: {e}")
        if session_logger:
            session_logger.log_error("OPENROUTER_ERROR", str(e))
        yield {"type": "text", "content": f"\n\n[OpenRouter error: {e}]"}


def sarvam_stream_request(
    messages: list,
    system_instruction: str,
    model: str,
    temperature: float = 0,
    session_logger=None
):
    """Stream a response from Sarvam AI (OpenAI-compatible API).

    Yields dicts with 'type' and 'content' keys.
    """
    config = load_config()
    sv_config = config.get("sarvam", {})
    api_key = os.environ.get("SARVAM_API_KEY") or sv_config.get("api_key", "")
    api_base = sv_config.get("api_base", "https://api.sarvam.ai/v1")

    if not api_key:
        logger.error("Sarvam API key not configured. Set SARVAM_API_KEY env var.")
        yield {"type": "text", "content": "Error: SARVAM_API_KEY environment variable not set."}
        return

    # Convert Gemini-format messages to OpenAI format
    oai_messages = []
    if system_instruction:
        oai_messages.append({"role": "system", "content": system_instruction})
    for msg in messages:
        role = msg.get("role", "user")
        if role == "model":
            role = "assistant"
        parts = msg.get("parts", [])
        content = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
        oai_messages.append({"role": role, "content": content})

    payload = {
        "model": model,
        "messages": oai_messages,
        "temperature": temperature,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        if session_logger:
            session_logger.log("SARVAM_REQUEST", {"model": model, "messages_count": len(oai_messages)})

        response = requests.post(
            f"{api_base}/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=120
        )
        response.raise_for_status()

        for line in response.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8") if isinstance(line, bytes) else line
            if decoded.startswith("data: "):
                decoded = decoded[6:]
            if decoded.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(decoded)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                text = delta.get("content", "")
                if text:
                    yield {"type": "text", "content": text}
            except json.JSONDecodeError:
                continue

    except Exception as e:
        logger.error(f"Sarvam stream error: {e}")
        if session_logger:
            session_logger.log_error("SARVAM_ERROR", str(e))
        yield {"type": "text", "content": f"\n\n[Sarvam error: {e}]"}


def get_chart_config(mcp_results: str, user_message: str, actual_dcids: list = None) -> dict:
    """Get chart configuration using structured output.

    Supports multiple charts for variables with different units/scales.
    """
    config = load_config()
    mcp_model = config.get("gemini", {}).get("mcp_model", "gemini-3-flash-preview")

    dcid_hint = ""
    if actual_dcids:
        dcid_hint = f"\n\n**ACTUAL DCIDs used in MCP tool calls (USE THESE EXACTLY):**\n{json.dumps(actual_dcids)}\nYou MUST use these exact DCIDs in variable_dcids. Do NOT substitute or invent different DCIDs.\n"

    prompt = f"""Based on the data query and results, determine chart configurations.

User Query: {user_message}
{dcid_hint}
Data Results:
{mcp_results if mcp_results else 'No data results'}

Instructions:
**CRITICAL — Year/Date Matching**: If the user's query mentions a specific year (e.g., "2023", "2021"), pass it via the appropriate date attribute for the chart type. Do NOT default to the latest/most recent year. Match the year from the user query exactly.

**CRITICAL — DCID Matching**: Use ONLY the variable DCIDs that appear in the Data Results or the ACTUAL DCIDs list above. Do NOT invent, modify, or substitute DCIDs. The chart MUST use the same DCIDs that the search/query used.

1. Extract variable DCIDs and place DCIDs from the results
2. Analyze variable units from source_metadata and data scales from the values
3. Group variables that can be meaningfully compared on the same Y-axis:
   - Same unit type (e.g., both INR, both counts, both percentages)
   - Similar magnitude (within ~100x of each other)
4. Create SEPARATE charts for incompatible variable groups:
   - Different unit types should be separate (e.g., "Count" vs "INR" vs "Percentage")
   - Vastly different scales should be separate (e.g., millions vs trillions)
5. MAXIMUM 3 charts - if more groups exist, prioritize most relevant to the query
6. Choose appropriate viz_type for each chart:
   - "map": USE THIS when data covers multiple Indian states/UTs for a SINGLE variable at a SINGLE point in time (state-wise comparisons, geographic distribution). Set parent_place="country/IND" and child_place_type="State". Include a date field.
   - "line": time series for one or more places/variables over multiple years
   - "bar": comparison across multiple places or variables at a single point in time (when map is not suitable)
   - "ranking": when ranking top/bottom states or entities
   PREFER "map" over "bar" whenever the data has 5+ Indian states with a single variable.
7. Give each chart a descriptive title related to data it is showing but do NOT include year/date in the title.
8. Date attributes per chart type (only these are supported by the web components):
   - "highlight": set `date` (ISO-8601: YYYY, YYYY-MM, or YYYY-MM-DD) when a specific year is mentioned
   - "map": set `date` (ISO-8601) when a specific year is mentioned
   - "line": set `startDate` and/or `endDate` (ISO-8601) to filter the time range. If user asks about a single year, set both startDate and endDate to that year. If a range (e.g. "2010 to 2020"), set startDate="2010" endDate="2020". Omit if no date constraint.
   - "slider": set `dates` as space-separated years (e.g. "2001 2002 2003") to define the slider range
   - "bar", "ranking", "pie", "gauge", "scatter": NO date attribute supported — do NOT set date for these

Set should_render to false if no meaningful data for visualization."""

    response = gemini_request(
        messages=[{"role": "user", "parts": [{"text": prompt}]}],
        system_instruction="You are a data visualization expert. Extract chart configurations from data results, grouping compatible variables together and separating incompatible ones into multiple charts.",
        model=mcp_model,
        temperature=0,
        thinking_level="minimal",  # Fastest for simple extraction
        response_schema=CHART_CONFIG_SCHEMA,
        stream=False
    )

    try:
        if "candidates" in response:
            text = response["candidates"][0]["content"]["parts"][0].get("text", "{}")
            chart_config = json.loads(text)
            logger.info(f"📊 Chart config result: {json.dumps(chart_config, indent=2)}")
            return chart_config
    except Exception as e:
        logger.error(f"Chart config parse error: {e}")

    return {"should_render": False}


# Schema for validating if synthesis response contains actual data
DATA_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "data_found": {
            "type": "boolean",
            "description": "True if the response contains actual data/statistics that answer the query. False if data is unavailable, not found, or the response says data doesn't exist."
        }
    },
    "required": ["data_found"]
}


def validate_data_response(synthesis_text: str, user_message: str) -> bool:
    """Quick validation: did synthesis actually answer with data?

    Called after synthesis completes to determine if charts should be shown.
    Uses fast model with no thinking for minimal latency.
    """
    config = load_config()
    model = config.get("gemini", {}).get("mcp_model", "gemini-2.0-flash")

    prompt = f"""User asked: {user_message}

Response given:
{synthesis_text[:2000]}

Did this response contain actual data/statistics that answer the user's question?
Return false if the response says data is "not available", "not found", "doesn't exist", or similar."""

    response = gemini_request(
        messages=[{"role": "user", "parts": [{"text": prompt}]}],
        system_instruction="You validate if a response contains actual data.",
        model=model,
        temperature=0,
        thinking_level="none",  # Fastest - no thinking needed
        response_schema=DATA_VALIDATION_SCHEMA,
        stream=False
    )

    try:
        if "candidates" in response:
            text = response["candidates"][0]["content"]["parts"][0].get("text", "{}")
            result = json.loads(text)
            return result.get("data_found", True)
    except Exception as e:
        logger.error(f"Data validation parse error: {e}")

    return True  # Default to showing charts on error


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    """Full chat workflow with SSE streaming.

    Phases:
    1. MCP Tools - Execute data queries (send tool call details)
    2. KB Query - Search knowledge base (if enabled)
    3. Synthesis - Stream final response with chart config

    Request body:
    {
        "message": "user query",
        "history": [...optional conversation history...],
        "session_id": "optional session ID for follow-up messages"
    }

    Query params (optional, requires valid key):
    - key: Secret key for config overrides (must match query_param_key in config)
    - model: Override mcp_model and kb_model
    - kb: "true" or "false" to toggle knowledge base
    - mcp_thinking: Override MCP thinking level
    - synthesis_thinking: Override synthesis thinking level

    Response: Server-Sent Events stream
    """
    global session_id  # MCP session ID

    data = request.get_json()
    if not data or not data.get("message"):
        return jsonify({"error": "Message required"}), 400

    user_message = data["message"]
    history = data.get("history", [])
    existing_session_id = data.get("session_id")  # From follow-up messages

    # ── Response Cache: check for any exact query match ──
    # Cache is keyed on normalized query text. We check regardless of session/history
    # because the same data query should return the same result.
    # Only *store* new entries for first-turn queries (no history, no session).
    is_cacheable = not history and not existing_session_id
    cached_events = _response_cache.get(user_message)
    if cached_events is not None:
            logger.info(f"CACHE HIT for query: {user_message[:80]}")

            def replay_cached():
                cache_start = time.time()
                cache_session = SessionLogger()
                yield f"data: {json.dumps({'session_id': cache_session.session_id})}\n\n"
                cache_session.log("CACHE_HIT", {"query": user_message[:200], "cached_events": len(cached_events)})

                for event in cached_events:
                    # Rewrite the done event with updated timing
                    if '"done": true' in event or '"done":true' in event:
                        try:
                            evt_data = json.loads(event.replace("data: ", "").strip())
                            evt_data['duration_ms'] = round((time.time() - cache_start) * 1000, 0)
                            evt_data['cached'] = True
                            yield f"data: {json.dumps(evt_data)}\n\n"
                        except Exception:
                            yield event if event.endswith("\n\n") else event + "\n\n"
                    else:
                        yield event if event.endswith("\n\n") else event + "\n\n"

                cache_session.log("CACHE_REPLAY_COMPLETE", {"duration_ms": round((time.time() - cache_start) * 1000, 1)})
                cache_session.flush()

            return Response(
                stream_with_context(replay_cached()),
                mimetype='text/event-stream',
                headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'}
            )

    # Query chaining: For follow-up messages, build a compact context block with:
    # 1. Previous user queries (preserve years, entities from vague follow-ups)
    # 2. Data availability from prior model responses (avoid re-querying dead ends)
    if history:
        prior_user_queries = []
        prior_data_context = []
        for msg in history:
            parts = msg.get("parts", [])
            if not parts:
                continue
            text = parts[0].get("text", "") if isinstance(parts[0], dict) else ""

            if msg.get("role") == "user" and text:
                prior_user_queries.append(text[:200])
            elif msg.get("role") == "model" and text:
                # Extract DCID mentions and data availability signals from model responses
                import re as _re
                dcid_matches = _re.findall(r'NDAP_\w+', text)
                if dcid_matches:
                    prior_data_context.append(f"Variables used: {', '.join(set(dcid_matches[:5]))}")
                if "not available" in text.lower() or "no data" in text.lower():
                    prior_data_context.append("Some requested data was not available in previous turns")

        context_block = ""
        if prior_user_queries:
            chain = ". ".join(prior_user_queries[-3:])  # Last 3 queries max
            context_block += f"[Previous queries: {chain}]\n"
        if prior_data_context:
            context_block += f"[Prior data context: {'; '.join(set(prior_data_context[:5]))}]\n"

        if context_block:
            user_message_for_mcp = f"{context_block}\nCurrent query: {user_message}"
        else:
            user_message_for_mcp = user_message
    else:
        user_message_for_mcp = user_message

    # S1: Intent-to-DCID pre-resolution — skip wasted search_indicators calls
    intent_result = resolve_intent_dcids(user_message)

    if intent_result["matched"] and intent_result["hint_text"]:
        user_message_for_mcp = f"{intent_result['hint_text']}\n\n{user_message_for_mcp}"

    # Parse query parameters for config overrides
    query_params = {}
    secret_key = request.args.get("key", "")
    expected_key = get_query_param_key()
    demo_mode = False

    if secret_key == expected_key:
        # Valid key - extract override params
        query_params = {
            "model": request.args.get("model"),  # e.g., "gemini-2.0-flash"
            "kb_enabled": request.args.get("kb"),  # "true" or "false"
            "mcp_thinking": request.args.get("mcp_thinking"),  # "low", "medium", "high", or budget number
            "synthesis_thinking": request.args.get("synthesis_thinking"),  # same options
        }
        # Remove None values
        query_params = {k: v for k, v in query_params.items() if v is not None}
        if query_params:
            logger.info(f"Query params override applied: {query_params}")

        # Check for demo mode - uses reserved API keys for internal demos
        if request.args.get("demo", "").lower() == "true":
            demo_mode = True
            logger.info("Demo mode ENABLED - using reserved demo API keys")
    elif secret_key:
        # Invalid key provided - log warning but continue with defaults
        logger.warning(f"Invalid query param key provided, ignoring overrides")

    # Create or resume session logger
    session_logger = SessionLogger(session_id=existing_session_id)

    def generate():
        nonlocal session_logger
        request_start_time = time.time()
        full_text = ""
        _cache_events = []  # Collect events for caching

        # Chart config runs in parallel with KB + synthesis
        chart_result_holder = {'config': {"should_render": False}}
        chart_thread = [None]  # Use list to avoid nonlocal issues

        # Send session ID first so frontend can display it
        yield f"data: {json.dumps({'session_id': session_logger.session_id})}\n\n"

        # Log query params if present
        if query_params:
            session_logger.log("QUERY_PARAMS_OVERRIDE", query_params)

        # Log demo mode if enabled
        if demo_mode:
            session_logger.log("DEMO_MODE_ENABLED", {"using_demo_keys": True})

        # Log user message
        session_logger.log_user_message(user_message, len(history))

        # Log intent resolution
        if intent_result["matched"]:
            session_logger.log("INTENT_RESOLVED", {
                "dcids": list(intent_result["dcids"].keys()),
                "kb_only": intent_result["kb_only"],
            })

        config = load_config()
        if not config:
            session_logger.log_error("CONFIG_ERROR", "Backend config not loaded")
            yield f"data: {json.dumps({'error': 'Backend config not loaded'})}\n\n"
            return

        # Apply query param overrides to config
        effective_config = apply_query_overrides(config, query_params)

        # Ensure MCP is initialized (fix for tool calls not showing)
        mcp_ready = False
        if not session_id:
            logger.info("MCP session not initialized, attempting to connect...")
            session_logger.log("MCP_INIT_ATTEMPT", {"reason": "session_id was None"})
            if initialize_mcp():
                session_logger.log("MCP_INIT_SUCCESS", {"mcp_session_id": session_id})
                mcp_ready = True
            else:
                # Even if init returns False, try to get tools anyway
                # Some MCP servers work without session IDs
                session_logger.log("MCP_INIT_RETURNED_FALSE", {"trying_tools_anyway": True})
        else:
            mcp_ready = True

        # Double-check: if we have tools, MCP is working regardless of session_id
        tools = get_tools()
        if tools:
            mcp_ready = True
            session_logger.log("MCP_TOOLS_AVAILABLE", {"tool_count": len(tools), "tools": [t.get("name") for t in tools]})
        else:
            session_logger.log("MCP_NO_TOOLS", {"session_id": session_id})

        # Create thought queue for streaming thoughts from background threads
        thought_queue = queue.Queue()

        def thought_callback(thought_text: str, phase: str):
            """Callback to put thoughts into queue for streaming."""
            thought_queue.put({'thought': thought_text, 'phase': phase})

        # S2: Session context — track working/empty variables across turns
        session_context = {
            'working_variables': [],   # DCIDs that returned data
            'empty_variables': [],     # DCIDs that returned no data
            'fallbacks_used': [],      # KB fallback topics
        }

        # Phase 1 + 2: MCP Tools + KB Query (run in PARALLEL)
        mcp_enabled = effective_config.get("mcp", {}).get("enabled", True)
        mcp_results = ""
        tool_calls_list = []
        data_status = None
        kb_response = ""
        kb_sources = []
        kb_enabled = effective_config.get("knowledge_base", {}).get("enabled", False)

        # Skip KB for pure data queries (saves 6-10s) — only use KB for policy/report questions
        if kb_enabled:
            _msg_lower = user_message.lower()
            _data_keywords = ['gdp', 'gsdp', 'gva', 'cpi', 'inflation', 'export', 'import', 'factory', 'factories',
                              'milk production', 'dairy production', 'unemployment', 'population density', 'birth rate',
                              'death rate', 'infant mortality', 'slum population', 'slum literacy', 'nhm allocation',
                              'worker population', 'tb notification', 'tb cases', 'dengue cases', 'census population',
                              'lfpr', 'worker ratio', 'labour participation', 'vital stats', 'mortality rate',
                              'population growth', 'population projection', 'aqi', 'pm2.5', 'pollution level',
                              'show', 'compare', 'trend', 'chart', 'graph', 'plot', 'map']
            _kb_keywords = ['policy', 'report', 'survey', 'nfhs', 'scheme', 'programme', 'program', 'recommend',
                            'why', 'explain', 'reason', 'cause', 'what should', 'suggest', 'guidelines', 'norms',
                            'act', 'regulation', 'rule', 'hindi', 'document', 'pdf', 'paper', 'study',
                            'economic survey', 'nss', 'health spending', 'health financing']
            _needs_kb = any(k in _msg_lower for k in _kb_keywords)
            _pure_data = any(k in _msg_lower for k in _data_keywords) and not _needs_kb
            if _pure_data:
                kb_enabled = False
                session_logger.log("KB_SKIPPED", {"reason": "pure data query", "query": user_message[:100]})

        # Start KB immediately in background (runs parallel with MCP)
        kb_result_holder = {'response': '', 'sources': []}
        kb_thread = None
        if kb_enabled:
            # S3: Build English retrieval query for better KB search
            kb_query = build_kb_retrieval_query(user_message, history)
            if kb_query != user_message:
                session_logger.log("KB_QUERY_ENRICHED", {"original": user_message[:100], "enriched": kb_query[:100]})

            def run_kb():
                try:
                    kb_result = execute_kb_query(
                        kb_query, session_logger=session_logger,
                        thought_callback=lambda t: thought_callback(t, 'kb'),
                        demo_mode=demo_mode,
                        effective_config=effective_config
                    )
                    kb_result_holder['response'] = kb_result.get("response", "")
                    kb_result_holder['sources'] = kb_result.get("sources", [])
                except Exception as e:
                    logger.error(f"KB thread error: {e}")

            kb_thread = threading.Thread(target=run_kb)
            kb_thread.start()

        if mcp_enabled and mcp_ready:
            yield f"data: {json.dumps({'status': 'mcp_start', 'message': 'Querying data tools...'})}\n\n"
            if kb_enabled:
                yield f"data: {json.dumps({'status': 'kb_start', 'message': 'Searching knowledge base...'})}\n\n"

            # Run MCP in thread to enable thought streaming
            mcp_result_holder = {'results': '', 'tool_calls': [], 'text': '', 'iterations': 0}

            def run_mcp():
                try:
                    mcp_result_holder['results'], mcp_result_holder['tool_calls'], mcp_result_holder['text'], mcp_result_holder['iterations'] = execute_mcp_tool_loop(
                        user_message_for_mcp, history, session_logger=session_logger,
                        effective_config=effective_config,
                        thought_callback=lambda t: thought_callback(t, 'mcp'),
                        demo_mode=demo_mode
                    )
                except Exception as e:
                    logger.error(f"MCP thread error: {e}")
                    mcp_result_holder['text'] = f"Error: {e}"

            mcp_thread = threading.Thread(target=run_mcp)
            mcp_thread.start()

            # Stream thoughts while MCP runs
            while mcp_thread.is_alive() or not thought_queue.empty():
                try:
                    thought_data = thought_queue.get(timeout=0.1)
                    yield f"data: {json.dumps(thought_data)}\n\n"
                except queue.Empty:
                    continue

            mcp_thread.join()

            # Signal MCP thinking complete
            yield f"data: {json.dumps({'thinking_complete': 'mcp'})}\n\n"

            # Get results from thread
            mcp_results = mcp_result_holder['results']
            tool_calls_list = mcp_result_holder['tool_calls']

            # Send each tool call for left sidebar
            for tc in tool_calls_list:
                yield f"data: {json.dumps({'type': 'tool_call', 'name': tc['name'], 'arguments': tc['arguments'], 'result': tc['result'], 'status': tc['status']})}\n\n"

            yield f"data: {json.dumps({'status': 'mcp_complete', 'tool_count': len(tool_calls_list)})}\n\n"

            # Check data availability and send status to frontend
            data_status = check_data_availability(tool_calls_list)
            yield f"data: {json.dumps({'data_status': data_status})}\n\n"

            # S2: Populate session context from tool results
            for tc in tool_calls_list:
                if tc['name'] == 'get_observations':
                    vd = tc['arguments'].get('variable_dcid', '')
                    if vd:
                        has_ts = bool(_RE_TIME_SERIES_HAS_DATA.search(tc.get('result', '')))
                        if has_ts:
                            session_context['working_variables'].append(vd)
                        else:
                            session_context['empty_variables'].append(vd)

            # Extract and send provenance sources from MCP results
            mcp_sources = extract_provenance_from_mcp_results(tool_calls_list)
            if mcp_sources:
                yield f"data: {json.dumps({'mcp_sources': mcp_sources})}\n\n"

            # Start chart config in background (runs parallel with synthesis)
            # Only generate charts if MCP actually found data (skip for KB-only answers)
            mcp_has_data = data_status.get('has_data', True) if data_status else True
            if mcp_results and mcp_has_data:
                # Extract actual DCIDs used in get_observations calls
                _actual_dcids = []
                for tc in tool_calls_list:
                    if tc['name'] == 'get_observations':
                        vd = tc['arguments'].get('variable_dcid', '')
                        if vd and vd not in _actual_dcids:
                            _actual_dcids.append(vd)
                def run_chart_config():
                    chart_result_holder['config'] = get_chart_config(mcp_results, user_message_for_mcp, _actual_dcids)
                chart_thread[0] = threading.Thread(target=run_chart_config)
                chart_thread[0].start()

        elif mcp_enabled and not mcp_ready:
            session_logger.log("MCP_SKIPPED", {"reason": "MCP not connected or no tools available"})
            yield f"data: {json.dumps({'status': 'mcp_skipped', 'message': 'MCP server not connected'})}\n\n"

        # Wait for KB to finish (it's been running in parallel with MCP)
        if kb_thread is not None:
            kb_thread.join()

            # Signal KB thinking complete
            yield f"data: {json.dumps({'thinking_complete': 'kb'})}\n\n"

            # Get results from thread
            kb_response = kb_result_holder['response']
            kb_sources = kb_result_holder['sources']

            # S4: Topic-coherence filter — drop off-topic KB responses
            kb_response, kb_sources = filter_kb_response_relevance(
                kb_response, kb_sources, user_message, history
            )
            if not kb_response and kb_result_holder['response']:
                session_logger.log("KB_FILTERED_OFFTOPIC", {"original_len": len(kb_result_holder['response'])})

            # Send KB sources to frontend for inline citations
            if kb_sources:
                yield f"data: {json.dumps({'kb_sources': kb_sources})}\n\n"
            yield f"data: {json.dumps({'status': 'kb_complete'})}\n\n"

        # Phase 3: Synthesis with streaming
        yield f"data: {json.dumps({'status': 'synthesis_start', 'message': 'Generating response...'})}\n\n"

        synthesis_prompt = effective_config.get("prompts", {}).get("synthesis", "")
        # Model priority: Gemini Flash (fastest) unless user explicitly selected another
        # URL param ?model=openrouter or ?model=sarvam overrides this default
        _sv_key = os.environ.get("SARVAM_API_KEY") or effective_config.get("sarvam", {}).get("api_key", "")
        _sv_model = effective_config.get("sarvam", {}).get("synthesis_model", "")
        _or_key = os.environ.get("OPENROUTER_API_KEY") or effective_config.get("openrouter", {}).get("api_key", "")
        _or_key = _or_key if _or_key and _or_key != "PASTE_YOUR_OPENROUTER_API_KEY_HERE" else ""
        _or_model = effective_config.get("openrouter", {}).get("synthesis_model", "")
        # Gemma 4 config (also via OpenRouter)
        _g4_config = effective_config.get("gemma4", {})
        _g4_key = _g4_config.get("api_key", "") or _or_key
        _g4_model = _g4_config.get("synthesis_model", "")
        # Check if user explicitly requested a model via URL param or body
        requested_model = request.args.get('model', '').lower() if request else ''
        if not requested_model:
            # Also check request body for model field
            try:
                body = request.get_json(silent=True) or {}
                requested_model = body.get('model', '').lower()
            except Exception:
                pass
        if requested_model == 'sarvam' and _sv_key and _sv_model:
            synthesis_model = _sv_model
        elif requested_model == 'gemma4' and _g4_key and _g4_model:
            synthesis_model = _g4_model
        elif requested_model == 'openrouter' and _or_key and _or_model:
            synthesis_model = _or_model
        elif _g4_key and _g4_model:
            # Default to Gemma 4 when available
            synthesis_model = _g4_model
        else:
            # Fallback to Gemini Flash
            synthesis_model = effective_config.get("gemini", {}).get("mcp_model", "gemini-3-flash-preview")
        thinking_level = effective_config.get("thinking", {}).get("synthesis_level", "low")

        # Build synthesis context with source labels for citations
        context_parts = []
        # Only include MCP data results if actual data was found
        # (avoids LLM mentioning "no data" when KB already answered the query)
        mcp_has_data = data_status.get('has_data', True) if data_status else True
        if mcp_results and mcp_has_data:
            # Format extracted sources as markdown links for synthesis
            mcp_sources = extract_provenance_from_mcp_results(tool_calls_list)
            if mcp_sources:
                source_links = ", ".join([f"[{s['name']}]({s['url']})" for s in mcp_sources])
            else:
                source_links = "[NDAP Data Commons](https://ndap.niti.gov.in/)"
            context_parts.append(f"**DATA RESULTS [Sources: {source_links}]:**\n{mcp_results}")
        if kb_response:
            # Include document names from kb_sources for proper citation
            if kb_sources:
                kb_source_names = ", ".join([s['title'] for s in kb_sources])
                context_parts.append(f"**POLICY INFORMATION [Sources: {kb_source_names}]:**\n(IMPORTANT: When citing this information, use the actual document names listed above as source citations — NEVER use the generic label 'Knowledge Base'.)\n{kb_response}")
            else:
                context_parts.append(f"**POLICY INFORMATION:**\n{kb_response}")
            session_context['fallbacks_used'].append('KB')

        # S2: Inject session context so LLM knows data availability
        if session_context['empty_variables']:
            context_parts.append(f"**DATA AVAILABILITY NOTE:** The following indicators returned NO data: {', '.join(session_context['empty_variables'])}. Do not reference these — only use data that was actually returned above.")

        # Log synthesis start
        session_logger.log_synthesis_start(["MCP" if mcp_results else None, "KB" if kb_response else None])

        # Detect query language for explicit enforcement
        _has_devanagari = bool(re.search(r'[\u0900-\u097F]', user_message))
        _has_latin_hindi = bool(re.search(r'\b(mein|kya|hai|ka|ke|ki|ko|se|aur|nahi|kaise|batao|kitna|kitne|kitni)\b', user_message.lower())) and not _has_devanagari
        if _has_devanagari:
            lang_instruction = "LANGUAGE INSTRUCTION: The user wrote in Devanagari Hindi. Respond ENTIRELY in Devanagari Hindi script."
        elif _has_latin_hindi:
            lang_instruction = "LANGUAGE INSTRUCTION: The user wrote in Roman/Hinglish script. Respond in Roman/Latin script Hindi. Do NOT use Devanagari characters."
        else:
            lang_instruction = "LANGUAGE INSTRUCTION: The user wrote in English. Respond ENTIRELY in English. Do NOT use Hindi, Devanagari, or any non-English text in your response."

        synthesis_message = f"""{lang_instruction}

User Query: {user_message}

{chr(10).join(context_parts) if context_parts else 'No additional context available.'}

Please provide a comprehensive response combining all available information. Remember: respond in the SAME language as the user query."""

        # Stream the synthesis response with thought streaming
        try:
            # Build messages with conversation history for context
            synthesis_messages = []

            # Add conversation history first (already in Gemini format from frontend)
            for msg in history:
                synthesis_messages.append(msg)

            # Add current query with MCP/KB context as final user message
            synthesis_messages.append({"role": "user", "parts": [{"text": synthesis_message}]})

            # Route based on model: Sarvam models start with "sarvam-", OpenRouter has "/", else Gemini
            _sv_key = os.environ.get("SARVAM_API_KEY") or load_config().get("sarvam", {}).get("api_key", "")
            if synthesis_model.startswith("sarvam-") and _sv_key:
                stream_gen = sarvam_stream_request(
                    messages=synthesis_messages,
                    system_instruction=synthesis_prompt,
                    model=synthesis_model,
                    temperature=0,
                    session_logger=session_logger
                )
            elif "/" in synthesis_model:
                stream_gen = openrouter_stream_request(
                    messages=synthesis_messages,
                    system_instruction=synthesis_prompt,
                    model=synthesis_model,
                    temperature=0,
                    session_logger=session_logger
                )
            else:
                stream_gen = gemini_request(
                    messages=synthesis_messages,
                    system_instruction=synthesis_prompt,
                    model=synthesis_model,
                    temperature=0,
                    thinking_level=thinking_level,
                    stream=True,
                    session_logger=session_logger,
                    include_thoughts=True,  # Enable thought streaming
                    demo_mode=demo_mode
                )

            if isinstance(stream_gen, dict) and "error" in stream_gen:
                session_logger.log_error("SYNTHESIS_ERROR", stream_gen['error'])
                yield f"data: {json.dumps({'error': stream_gen['error']})}\n\n"
                return

            for chunk in stream_gen:
                # Handle dict format with 'type' and 'content' keys
                if isinstance(chunk, dict):
                    if chunk.get('type') == 'thought':
                        yield f"data: {json.dumps({'thought': chunk['content'], 'phase': 'synthesis'})}\n\n"
                    elif chunk.get('type') == 'text':
                        full_text += chunk['content']
                        yield f"data: {json.dumps({'text': chunk['content']})}\n\n"
                else:
                    # Backward compatibility: plain text string
                    full_text += chunk
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

            # Signal synthesis thinking complete
            yield f"data: {json.dumps({'thinking_complete': 'synthesis'})}\n\n"

        except Exception as e:
            logger.error(f"Synthesis streaming error: {e}")
            session_logger.log_error("SYNTHESIS_STREAM_ERROR", str(e))
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        # Quick validation: should we show charts based on synthesis response?
        show_charts = True
        if full_text and chart_thread[0]:
            show_charts = validate_data_response(full_text, user_message)
            if not show_charts:
                session_logger.log("CHART_VALIDATION", {"data_found": False, "action": "hide_charts"})

        # Wait for chart config thread (started after MCP, runs parallel with KB + synthesis)
        if chart_thread[0]:
            chart_thread[0].join(timeout=5)
        chart_config = chart_result_holder['config']

        # Add hide_charts flag if validation determined no data was found
        if not show_charts:
            chart_config['hide_charts'] = True

        # Log final response and flush buffered log entries to disk
        total_duration_ms = (time.time() - request_start_time) * 1000
        session_logger.log_final_response(full_text, chart_config, total_duration_ms)
        session_logger.flush()

        # GP-10: Estimate token usage and cost for display
        # 1 token ≈ 4 chars for English, ~2.5 for Hindi — use 4 as conservative estimate
        input_chars = len(user_message) + len(mcp_results) + len(kb_response) + len(synthesis_message)
        output_chars = len(full_text)
        input_tokens_est = input_chars // 4
        output_tokens_est = output_chars // 4
        total_tokens_est = input_tokens_est + output_tokens_est
        # Gemini Flash pricing: $0.075/1M input tokens, $0.30/1M output tokens
        cost_usd_est = (input_tokens_est * 0.075 + output_tokens_est * 0.30) / 1_000_000

        # Count unique indicators (stat vars) searched via get_observations
        indicators_searched = set()
        for tc in tool_calls_list:
            if tc.get('name') == 'get_observations':
                vd = tc.get('arguments', {}).get('variable_dcid', '')
                if vd:
                    indicators_searched.add(vd)

        # Send final event with timing + cost info (GP-10)
        try:
            mcp_iterations_used = mcp_result_holder.get('iterations', 0)
        except NameError:
            mcp_iterations_used = 0
        yield f"data: {json.dumps({'chart_config': chart_config, 'done': True, 'duration_ms': round(total_duration_ms, 0), 'total_tokens': total_tokens_est, 'input_tokens': input_tokens_est, 'output_tokens': output_tokens_est, 'cost_usd': round(cost_usd_est, 6), 'indicators_searched': len(indicators_searched), 'tool_calls_count': len(tool_calls_list), 'mcp_iterations': mcp_iterations_used})}\n\n"

    def generate_and_cache():
        """Wrap generate() to collect cacheable events (tool calls, text, charts, done)."""
        cacheable_events = []
        for event in generate():
            yield event
            # Only cache substantive events (skip session_id, status updates, thinking)
            if is_cacheable and event.startswith("data: "):
                try:
                    evt_str = event.strip()
                    evt_data = json.loads(evt_str.replace("data: ", "", 1))
                    # Cache: text chunks, tool_call details, chart_config/done, mcp_sources, data_status
                    if any(k in evt_data for k in ('text', 'done', 'type', 'mcp_sources', 'data_status', 'chart_config')):
                        cacheable_events.append(evt_str)
                    # Store cache when we see the done event (don't wait for generator to finish,
                    # as Flask may not fully consume the generator after client disconnects)
                    if evt_data.get('done'):
                        _response_cache.put(user_message, cacheable_events)
                        logger.info(f"CACHE STORE: {len(cacheable_events)} events for query: {user_message[:80]}")
                except Exception:
                    pass

    return Response(
        stream_with_context(generate_and_cache()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


# ============================================================
# LOGS ANALYTICS DASHBOARD
# ============================================================

def parse_log_file(log_path: Path) -> dict:
    """Parse a single log file and extract metrics for analytics.

    Returns:
        dict with session_id, query, timestamp, status, duration_ms,
        tool_calls, errors, model, thinking_level, kb_enabled
    """
    result = {
        "session_id": log_path.stem,
        "query": None,
        "timestamp": None,
        "status": "unknown",
        "duration_ms": None,
        "tool_calls": [],
        "stat_vars": [],  # List of unique stat vars fetched
        "errors": [],
        "model": None,
        "thinking_level": None,
        "kb_enabled": False,
        "text_length": 0
    }

    def process_event(event_name: str, data_lines: list):
        """Process a single event's data."""
        if not event_name or not data_lines:
            return
        try:
            data_str = '\n'.join(data_lines)
            data = json.loads(data_str)

            if event_name == 'USER_MESSAGE':
                result['query'] = data.get('message', '')
            elif event_name == 'GEMINI_REQUEST':
                if not result['model']:
                    result['model'] = data.get('model', '')
                if not result['thinking_level']:
                    payload = data.get('payload', {})
                    result['thinking_level'] = payload.get('thinking_level', '')
            elif event_name == 'MCP_TOOL_REQUEST':
                tool_name = data.get('tool_name', '')
                arguments = data.get('arguments', {})
                result['tool_calls'].append({
                    'name': tool_name,
                    'arguments': arguments
                })
                # Extract stat var from get_observations calls
                if tool_name == 'get_observations':
                    var_dcid = arguments.get('variable_dcid', '')
                    if var_dcid and var_dcid not in result['stat_vars']:
                        result['stat_vars'].append(var_dcid)
            elif event_name == 'MCP_TOOL_RESPONSE':
                if result['tool_calls']:
                    result['tool_calls'][-1]['status'] = data.get('status', 'unknown')
                    result['tool_calls'][-1]['duration_ms'] = data.get('duration_ms', 0)
            elif event_name == 'ERROR':
                result['errors'].append({
                    'type': data.get('error_type', ''),
                    'message': data.get('error_message', '')
                })
            elif event_name == 'KB_QUERY':
                result['kb_enabled'] = True
            elif event_name == 'QUERY_PARAMS_OVERRIDE':
                if data.get('kb_enabled') == 'true':
                    result['kb_enabled'] = True
            elif event_name == 'FINAL_RESPONSE':
                result['duration_ms'] = data.get('total_duration_ms')
                result['text_length'] = data.get('text_length', 0)
        except json.JSONDecodeError:
            pass

    try:
        with open(log_path, 'r') as f:
            content = f.read()

        # Parse each event block
        current_event = None
        current_data = []

        for line in content.split('\n'):
            # Check for event header
            if line.startswith('--- ') and ' @ ' in line:
                # Process previous event BEFORE starting new one
                process_event(current_event, current_data)

                # Extract event type from header
                parts = line.split(' @ ')
                current_event = parts[0].replace('--- ', '').strip()
                if len(parts) > 1:
                    timestamp_str = parts[1].replace(' ---', '').strip()
                    if not result['timestamp']:
                        result['timestamp'] = timestamp_str
                current_data = []
            elif line.startswith('{') or (current_data and not line.startswith('=')):
                current_data.append(line)

        # IMPORTANT: Process the LAST event (usually FINAL_RESPONSE)
        process_event(current_event, current_data)

        # Determine success/failure status
        if result['errors']:
            result['status'] = 'failed'
        elif result['text_length'] and result['text_length'] > 0:
            result['status'] = 'success'
        elif result['duration_ms'] and result['duration_ms'] > 0:
            result['status'] = 'success'
        else:
            result['status'] = 'unknown'

    except Exception as e:
        logger.error(f"Error parsing log file {log_path}: {e}")
        result['status'] = 'parse_error'

    return result


def calculate_percentiles(values: list) -> dict:
    """Calculate response time percentiles."""
    if not values:
        return {"p50": 0, "p75": 0, "p90": 0, "p95": 0, "p99": 0}

    sorted_values = sorted(values)
    n = len(sorted_values)

    def percentile(p):
        k = (n - 1) * p / 100
        f = int(k)
        c = f + 1 if f + 1 < n else f
        return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f]) if c != f else sorted_values[f]

    return {
        "p50": round(percentile(50), 0),
        "p75": round(percentile(75), 0),
        "p90": round(percentile(90), 0),
        "p95": round(percentile(95), 0),
        "p99": round(percentile(99), 0)
    }


def get_all_logs_analytics() -> dict:
    """Aggregate analytics from all log files in the logs folder."""
    logs_dir = Path(__file__).parent / 'logs'

    if not logs_dir.exists():
        return {"error": "Logs directory not found"}

    log_files = sorted(logs_dir.glob('*.log'), reverse=True)

    # Parse all logs
    parsed_logs = []
    for log_file in log_files:
        parsed = parse_log_file(log_file)
        if parsed['query']:  # Only include logs with actual queries
            parsed_logs.append(parsed)

    # Calculate aggregated stats
    total = len(parsed_logs)
    successful = sum(1 for p in parsed_logs if p['status'] == 'success')
    failed = sum(1 for p in parsed_logs if p['status'] == 'failed')
    unknown = sum(1 for p in parsed_logs if p['status'] in ('unknown', 'parse_error'))

    # Response times
    durations = [p['duration_ms'] for p in parsed_logs if p['duration_ms'] is not None]
    avg_duration = sum(durations) / len(durations) if durations else 0
    percentiles = calculate_percentiles(durations)

    # By date
    by_date = {}
    for p in parsed_logs:
        if p['timestamp']:
            date = p['timestamp'][:10]  # Extract YYYY-MM-DD
            if date not in by_date:
                by_date[date] = {"queries": 0, "successful": 0, "failed": 0}
            by_date[date]["queries"] += 1
            if p['status'] == 'success':
                by_date[date]["successful"] += 1
            elif p['status'] == 'failed':
                by_date[date]["failed"] += 1

    # MCP tools summary
    tool_counts = {}
    total_tool_calls = 0
    for p in parsed_logs:
        for tc in p['tool_calls']:
            name = tc.get('name', 'unknown')
            tool_counts[name] = tool_counts.get(name, 0) + 1
            total_tool_calls += 1

    # Model stats
    model_counts = {}
    for p in parsed_logs:
        model = p['model'] or 'unknown'
        model_counts[model] = model_counts.get(model, 0) + 1

    # Config stats
    kb_enabled_count = sum(1 for p in parsed_logs if p['kb_enabled'])
    thinking_levels = {}
    for p in parsed_logs:
        level = p['thinking_level'] or 'unknown'
        thinking_levels[level] = thinking_levels.get(level, 0) + 1

    # Error summary
    error_types = {}
    for p in parsed_logs:
        for e in p['errors']:
            etype = e.get('type', 'unknown')
            error_types[etype] = error_types.get(etype, 0) + 1

    # Recent queries (last 50)
    recent_queries = []
    for p in parsed_logs[:50]:
        # Map 'unknown' status to 'stopped' for display
        status = 'stopped' if p['status'] == 'unknown' else p['status']
        recent_queries.append({
            "session_id": p['session_id'],
            "query": p['query'][:100] + "..." if p['query'] and len(p['query']) > 100 else p['query'],
            "full_query": p['query'],
            "timestamp": p['timestamp'],
            "status": status,
            "duration_ms": p['duration_ms'],
            "tool_count": len(p['tool_calls']),
            "tool_calls": p['tool_calls'],
            "stat_vars": p.get('stat_vars', []),
            "model": p['model'],
            "kb_enabled": p['kb_enabled']
        })

    return {
        "total_queries": total,
        "successful": successful,
        "failed": failed,
        "stopped": unknown,  # Renamed from 'unknown' to 'stopped'
        "success_rate": round(successful / total * 100, 1) if total > 0 else 0,
        "response_times": {
            "avg_ms": round(avg_duration, 0),
            **percentiles
        },
        "by_date": dict(sorted(by_date.items())),
        "mcp_summary": {
            "total_calls": total_tool_calls,
            "by_tool": tool_counts,
            "avg_per_query": round(total_tool_calls / total, 1) if total > 0 else 0
        },
        "error_summary": error_types,
        "recent_queries": recent_queries,
        "generated_at": datetime.now().isoformat()
    }


@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    """Store user feedback (thumbs up/down) for a response."""
    data = request.get_json(silent=True) or {}
    vote = data.get("vote")
    session_id = data.get("session")
    if not vote:
        return jsonify({"error": "Missing vote"}), 400
    # Append to feedback log file
    feedback_entry = {
        "vote": vote,
        "session_id": session_id,
        "text_preview": data.get("text", "")[:100],
        "timestamp": datetime.now().isoformat()
    }
    feedback_path = Path("logs/feedback.jsonl")
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    with open(feedback_path, "a") as f:
        f.write(json.dumps(feedback_entry) + "\n")
    logger.info(f"Feedback recorded: {vote} for session {session_id}")
    return jsonify({"success": True})


@app.route("/api/bug-report", methods=["POST"])
def submit_bug_report():
    """Store user bug reports with full session context."""
    data = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()
    if not description or len(description) < 5:
        return jsonify({"error": "Description too short"}), 400
    bug_entry = {
        "id": str(uuid.uuid4())[:8],
        "category": data.get("category", "other"),
        "description": description,
        "expected": data.get("expected", ""),
        "session_id": data.get("session"),
        "recent_queries": data.get("recent_queries", []),
        "url": data.get("url", ""),
        "user_agent": data.get("user_agent", ""),
        "screen": data.get("screen", ""),
        "reported_at": data.get("timestamp", datetime.now().isoformat()),
        "server_ts": datetime.now().isoformat()
    }
    bug_path = Path("logs/bug_reports.jsonl")
    bug_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bug_path, "a") as f:
        f.write(json.dumps(bug_entry) + "\n")
    logger.info(f"Bug report #{bug_entry['id']}: [{bug_entry['category']}] {description[:60]}")
    return jsonify({"success": True, "id": bug_entry["id"]})


@app.route("/api/logs/analytics")
def logs_analytics():
    """API endpoint for logs analytics - requires ?key=<secret_key>."""
    secret_key = request.args.get("key", "")
    expected_key = get_query_param_key()

    if secret_key != expected_key:
        return jsonify({"error": "Invalid or missing key parameter"}), 401

    analytics = get_all_logs_analytics()
    return jsonify({"success": True, **analytics})


@app.route("/api/logs/session/<session_id>")
def logs_session_detail(session_id):
    """API endpoint for single session details - requires ?key=<secret_key>."""
    secret_key = request.args.get("key", "")
    expected_key = get_query_param_key()

    if secret_key != expected_key:
        return jsonify({"error": "Invalid or missing key parameter"}), 401

    logs_dir = Path(__file__).parent / 'logs'
    log_file = logs_dir / f"{session_id}.log"

    if not log_file.exists():
        return jsonify({"error": "Session not found"}), 404

    parsed = parse_log_file(log_file)

    # Also include raw log content
    try:
        with open(log_file, 'r') as f:
            raw_content = f.read()
    except:
        raw_content = ""

    return jsonify({
        "success": True,
        "session": parsed,
        "raw_log": raw_content
    })


@app.route("/api/logs/session/<session_id>/download")
def logs_session_download(session_id):
    """Download a clean, evaluator-friendly execution log for a session.
    No auth required — session ID is the access token.
    Returns a plain-text file showing every step the system took.
    """
    logs_dir = Path(__file__).parent / 'logs'
    log_file = logs_dir / f"{session_id}.log"

    if not log_file.exists():
        return jsonify({"error": "Session not found"}), 404

    parsed = parse_log_file(log_file)

    lines = []
    lines.append("=" * 70)
    lines.append("NDAP DATA AGENT — EXECUTION LOG")
    lines.append("=" * 70)
    lines.append(f"Session ID  : {session_id}")
    lines.append(f"Started     : {parsed.get('timestamp', 'unknown')}")
    lines.append(f"Model       : {parsed.get('model', 'unknown')}")
    lines.append(f"KB Enabled  : {parsed.get('kb_enabled', False)}")
    lines.append(f"Status      : {parsed.get('status', 'unknown')}")
    if parsed.get('duration_ms'):
        lines.append(f"Total Time  : {parsed['duration_ms']/1000:.1f}s")
    lines.append("")

    lines.append("USER QUERY")
    lines.append("-" * 40)
    lines.append(parsed.get('query') or "(not captured)")
    lines.append("")

    tool_calls = parsed.get('tool_calls', [])
    if tool_calls:
        lines.append(f"MCP TOOL CALLS ({len(tool_calls)} total)")
        lines.append("-" * 40)
        for i, tc in enumerate(tool_calls, 1):
            lines.append(f"  [{i}] Tool    : {tc.get('name', '')}")
            args = tc.get('arguments', {})
            if 'query' in args:
                lines.append(f"      Query   : {args['query']}")
            if 'variable_dcid' in args:
                lines.append(f"      Variable: {args['variable_dcid']}")
            if 'entity_dcids' in args:
                entities = args['entity_dcids']
                if isinstance(entities, list):
                    lines.append(f"      Entities: {', '.join(entities[:5])}")
            lines.append(f"      Status  : {tc.get('status', 'unknown')}")
            if tc.get('duration_ms'):
                lines.append(f"      Duration: {tc['duration_ms']:.0f}ms")
            lines.append("")

    stat_vars = parsed.get('stat_vars', [])
    if stat_vars:
        lines.append(f"STATISTICAL VARIABLES FETCHED ({len(stat_vars)})")
        lines.append("-" * 40)
        for sv in stat_vars:
            lines.append(f"  - {sv}")
        lines.append("")

    errors = parsed.get('errors', [])
    if errors:
        lines.append(f"ERRORS ({len(errors)})")
        lines.append("-" * 40)
        for e in errors:
            lines.append(f"  [{e.get('type','')}] {e.get('message','')}")
        lines.append("")

    lines.append("=" * 70)
    lines.append("RAW LOG (full detail)")
    lines.append("=" * 70)
    try:
        with open(log_file, 'r') as f:
            lines.append(f.read())
    except:
        lines.append("(raw log unavailable)")

    output = "\n".join(lines)
    filename = f"ndap_execution_log_{session_id}.txt"
    return output, 200, {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{filename}"'
    }


@app.route("/logs")
def logs_dashboard():
    """HTML dashboard for logs analytics - requires ?key=<secret_key>."""
    secret_key = request.args.get("key", "")
    expected_key = get_query_param_key()

    if secret_key != expected_key:
        return """
        <html>
        <head><title>Access Denied</title></head>
        <body style="background: #f8f9fa; color: #3C4043; font-family: 'Google Sans', system-ui, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0;">
            <div style="text-align: center; background: white; padding: 40px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                <h1 style="color: #EA4335; margin-bottom: 16px;">Access Denied</h1>
                <p style="color: #5f6368;">Please provide a valid key parameter: /logs?key=YOUR_KEY</p>
            </div>
        </body>
        </html>
        """, 401

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Query Analytics Dashboard</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            /* Google Colors - Light Mode Theme */
            :root {{
                --google-blue: #4285F4;
                --google-red: #EA4335;
                --google-yellow: #FBBC04;
                --google-green: #34A853;
                --text-dark: #3C4043;
                --text-muted: #5f6368;
                --bg-light: #f8f9fa;
                --bg-white: #ffffff;
                --border-color: #dadce0;
            }}
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                background: var(--bg-light);
                color: var(--text-dark);
                font-family: 'Google Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                padding: 20px;
                min-height: 100vh;
            }}
            .header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 24px;
                padding-bottom: 16px;
                border-bottom: 1px solid var(--border-color);
            }}
            .header h1 {{ font-size: 24px; color: var(--text-dark); }}
            .header-actions {{ display: flex; gap: 12px; align-items: center; }}
            .refresh-btn {{
                background: var(--google-blue);
                color: #fff;
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 14px;
            }}
            .refresh-btn:hover {{ background: #3367d6; }}
            .auto-refresh {{ font-size: 12px; color: var(--text-muted); }}
            .last-updated {{ font-size: 12px; color: var(--text-muted); }}

            .cards {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                gap: 16px;
                margin-bottom: 24px;
            }}
            .card {{
                background: var(--bg-white);
                border-radius: 12px;
                padding: 20px;
                border: 1px solid var(--border-color);
                box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            }}
            .card-label {{ font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; }}
            .card-value {{ font-size: 32px; font-weight: 700; color: var(--text-dark); margin: 8px 0; }}
            .card-sub {{ font-size: 14px; color: var(--text-muted); }}
            .card.success .card-value {{ color: var(--google-green); }}
            .card.error .card-value {{ color: var(--google-red); }}
            .card.warning .card-value {{ color: var(--google-yellow); }}

            .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; margin-bottom: 24px; }}
            @media (max-width: 1200px) {{ .grid {{ grid-template-columns: 1fr; }} }}

            .panel {{
                background: var(--bg-white);
                border-radius: 12px;
                padding: 20px;
                border: 1px solid var(--border-color);
                box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            }}
            .panel-title {{ font-size: 16px; font-weight: 600; margin-bottom: 16px; color: var(--text-dark); }}

            .chart-container {{ height: 250px; }}

            .tool-bar {{
                display: flex;
                align-items: center;
                margin-bottom: 8px;
            }}
            .tool-name {{ width: 160px; font-size: 13px; color: var(--text-dark); }}
            .tool-progress {{
                flex: 1;
                height: 20px;
                background: #e8eaed;
                border-radius: 4px;
                overflow: hidden;
                margin: 0 12px;
            }}
            .tool-fill {{
                height: 100%;
                background: linear-gradient(90deg, var(--google-blue), #5a9cf8);
                border-radius: 4px;
            }}
            .tool-count {{ width: 80px; text-align: right; font-size: 13px; color: var(--text-muted); }}

            .stats-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
            .stat-item {{ padding: 12px; background: var(--bg-light); border-radius: 8px; }}
            .stat-label {{ font-size: 11px; color: var(--text-muted); }}
            .stat-value {{ font-size: 18px; font-weight: 600; color: var(--text-dark); }}

            .table-container {{ overflow-x: auto; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
            th {{ text-align: left; padding: 12px 8px; border-bottom: 2px solid var(--border-color); color: var(--text-muted); font-weight: 500; }}
            td {{ padding: 12px 8px; border-bottom: 1px solid var(--border-color); }}
            tr:hover {{ background: var(--bg-light); }}
            .status-success {{ color: var(--google-green); font-weight: 600; }}
            .status-failed {{ color: var(--google-red); font-weight: 600; }}
            .status-stopped {{ color: var(--google-yellow); font-weight: 600; }}
            .stat-vars-cell {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--text-muted); }}
            .query-text {{ max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            .expandable {{ cursor: pointer; }}
            .tool-details {{
                display: none;
                padding: 12px;
                background: var(--bg-light);
                margin: 4px 0;
                border-radius: 6px;
                font-size: 12px;
                border: 1px solid var(--border-color);
            }}
            .tool-details.show {{ display: block; }}
            .filter-row {{
                display: flex;
                gap: 12px;
                margin-bottom: 16px;
                flex-wrap: wrap;
                align-items: center;
            }}
            .search-box {{
                flex: 1;
                min-width: 200px;
                padding: 10px 14px;
                background: var(--bg-white);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                color: var(--text-dark);
                font-size: 14px;
            }}
            .search-box::placeholder {{ color: var(--text-muted); }}
            .search-box:focus {{ outline: none; border-color: var(--google-blue); box-shadow: 0 0 0 2px rgba(66,133,244,0.2); }}
            .date-input {{
                padding: 10px 14px;
                background: var(--bg-white);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                color: var(--text-dark);
                font-size: 14px;
            }}
            .date-input:focus {{ outline: none; border-color: var(--google-blue); box-shadow: 0 0 0 2px rgba(66,133,244,0.2); }}
            .filter-label {{ font-size: 12px; color: var(--text-muted); }}

            .percentile-bar {{
                display: flex;
                align-items: center;
                margin-bottom: 8px;
            }}
            .percentile-label {{ width: 50px; font-size: 12px; color: var(--text-muted); }}
            .percentile-track {{
                flex: 1;
                height: 24px;
                background: #e8eaed;
                border-radius: 4px;
                position: relative;
                overflow: hidden;
            }}
            .percentile-fill {{
                height: 100%;
                background: linear-gradient(90deg, var(--google-green), #45c362);
                border-radius: 4px;
                display: flex;
                align-items: center;
                justify-content: flex-end;
                padding-right: 8px;
                font-size: 11px;
                color: #fff;
                font-weight: 500;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Query Analytics Dashboard</h1>
            <div class="header-actions">
                <span class="last-updated" id="lastUpdated">Loading...</span>
                <label class="auto-refresh">
                    <input type="checkbox" id="autoRefresh" checked> Auto-refresh (30s)
                </label>
                <button class="refresh-btn" onclick="loadData()">Refresh</button>
            </div>
        </div>

        <div class="cards" id="summaryCards">
            <div class="card"><div class="card-label">Total Queries</div><div class="card-value" id="totalQueries">-</div></div>
            <div class="card success"><div class="card-label">Successful</div><div class="card-value" id="successful">-</div><div class="card-sub" id="successRate">-</div></div>
            <div class="card error"><div class="card-label">Failed</div><div class="card-value" id="failed">-</div></div>
            <div class="card warning"><div class="card-label">Stopped</div><div class="card-value" id="stopped">-</div></div>
            <div class="card"><div class="card-label">Avg Response</div><div class="card-value" id="avgTime">-</div><div class="card-sub">seconds</div></div>
            <div class="card"><div class="card-label">p95 Response</div><div class="card-value" id="p95Time">-</div><div class="card-sub">seconds</div></div>
        </div>

        <div class="grid">
            <div class="panel">
                <div class="panel-title">Queries by Day</div>
                <div class="chart-container"><canvas id="dailyChart"></canvas></div>
            </div>
            <div class="panel">
                <div class="panel-title">MCP Tool Usage</div>
                <div id="toolBars"></div>
                <div class="stats-grid" style="margin-top: 16px;">
                    <div class="stat-item"><div class="stat-label">Total Calls</div><div class="stat-value" id="totalCalls">-</div></div>
                    <div class="stat-item"><div class="stat-label">Avg per Query</div><div class="stat-value" id="avgCalls">-</div></div>
                </div>
            </div>
        </div>

        <div class="panel" style="margin-bottom: 24px;">
            <div class="panel-title">Response Time Percentiles</div>
            <div id="percentileBars"></div>
        </div>

        <div class="panel">
            <div class="panel-title">Recent Queries</div>
            <div class="filter-row">
                <input type="text" class="search-box" id="searchBox" placeholder="Search queries..." oninput="filterQueries()">
                <span class="filter-label">From:</span>
                <input type="date" class="date-input" id="dateFrom" onchange="filterQueries()">
                <span class="filter-label">To:</span>
                <input type="date" class="date-input" id="dateTo" onchange="filterQueries()">
                <button class="refresh-btn" onclick="clearDateFilter()" style="background: #5f6368;">Clear Dates</button>
            </div>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Date/Time</th>
                            <th>Query</th>
                            <th>Stat Vars</th>
                            <th>Status</th>
                            <th>Duration</th>
                            <th>Tools</th>
                        </tr>
                    </thead>
                    <tbody id="queriesTable"></tbody>
                </table>
            </div>
        </div>

        <script>
            const API_KEY = '{secret_key}';
            let analyticsData = null;
            let dailyChart = null;
            let autoRefreshInterval = null;

            async function loadData() {{
                try {{
                    const res = await fetch('/api/logs/analytics?key=' + API_KEY);
                    const data = await res.json();
                    if (data.success) {{
                        analyticsData = data;
                        renderDashboard(data);
                        document.getElementById('lastUpdated').textContent = 'Updated: ' + new Date().toLocaleTimeString();
                    }}
                }} catch (e) {{
                    console.error('Failed to load data:', e);
                }}
            }}

            function renderDashboard(data) {{
                // Summary cards
                document.getElementById('totalQueries').textContent = data.total_queries;
                document.getElementById('successful').textContent = data.successful;
                document.getElementById('successRate').textContent = data.success_rate + '% success';
                document.getElementById('failed').textContent = data.failed;
                document.getElementById('stopped').textContent = data.stopped || 0;
                document.getElementById('avgTime').textContent = (data.response_times.avg_ms / 1000).toFixed(1);
                document.getElementById('p95Time').textContent = (data.response_times.p95 / 1000).toFixed(1);

                // Daily chart
                renderDailyChart(data.by_date);

                // Tool bars
                renderToolBars(data.mcp_summary);
                document.getElementById('totalCalls').textContent = data.mcp_summary.total_calls;
                document.getElementById('avgCalls').textContent = data.mcp_summary.avg_per_query;

                // Percentile bars
                renderPercentileBars(data.response_times);

                // Queries table
                renderQueriesTable(data.recent_queries);
            }}

            // Google Colors
            const GOOGLE_COLORS = {{
                blue: '#4285F4',
                red: '#EA4335',
                yellow: '#FBBC05',
                green: '#34A853'
            }};

            function renderDailyChart(byDate) {{
                const labels = Object.keys(byDate).slice(-14);
                const successData = labels.map(d => byDate[d].successful);
                const failedData = labels.map(d => byDate[d].failed);

                const ctx = document.getElementById('dailyChart').getContext('2d');
                if (dailyChart) dailyChart.destroy();

                dailyChart = new Chart(ctx, {{
                    type: 'bar',
                    data: {{
                        labels: labels.map(d => d.slice(5)),
                        datasets: [
                            {{ label: 'Success', data: successData, backgroundColor: GOOGLE_COLORS.green }},
                            {{ label: 'Failed', data: failedData, backgroundColor: GOOGLE_COLORS.red }}
                        ]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {{
                            x: {{ stacked: true, grid: {{ color: '#e8eaed' }}, ticks: {{ color: '#5f6368' }} }},
                            y: {{ stacked: true, grid: {{ color: '#e8eaed' }}, ticks: {{ color: '#5f6368' }} }}
                        }},
                        plugins: {{ legend: {{ labels: {{ color: '#5f6368' }} }} }}
                    }}
                }});
            }}

            function renderToolBars(mcpSummary) {{
                const container = document.getElementById('toolBars');
                const maxCount = Math.max(...Object.values(mcpSummary.by_tool));

                container.innerHTML = Object.entries(mcpSummary.by_tool)
                    .sort((a, b) => b[1] - a[1])
                    .map(([name, count]) => `
                        <div class="tool-bar">
                            <span class="tool-name">${{name}}</span>
                            <div class="tool-progress">
                                <div class="tool-fill" style="width: ${{count / maxCount * 100}}%"></div>
                            </div>
                            <span class="tool-count">${{count}} calls</span>
                        </div>
                    `).join('');
            }}

            function renderPercentileBars(times) {{
                const container = document.getElementById('percentileBars');
                const maxTime = times.p99 || 1;
                const percentiles = ['p50', 'p75', 'p90', 'p95', 'p99'];

                container.innerHTML = percentiles.map(p => `
                    <div class="percentile-bar">
                        <span class="percentile-label">${{p}}</span>
                        <div class="percentile-track">
                            <div class="percentile-fill" style="width: ${{(times[p] / maxTime * 100)}}%">
                                ${{(times[p] / 1000).toFixed(1)}}s
                            </div>
                        </div>
                    </div>
                `).join('');
            }}

            function renderQueriesTable(queries) {{
                const tbody = document.getElementById('queriesTable');
                tbody.innerHTML = queries.map((q, i) => {{
                    let statusClass = 'status-stopped';
                    let statusSymbol = '⏹';
                    if (q.status === 'success') {{
                        statusClass = 'status-success';
                        statusSymbol = '✓';
                    }} else if (q.status === 'failed') {{
                        statusClass = 'status-failed';
                        statusSymbol = '✗';
                    }}
                    // Format stat vars for display
                    const statVars = q.stat_vars || [];
                    const statVarsDisplay = statVars.length > 0
                        ? statVars.slice(0, 2).join(', ') + (statVars.length > 2 ? ` (+${{statVars.length - 2}})` : '')
                        : '-';
                    const statVarsTitle = statVars.join('\\n');
                    return `
                    <tr class="expandable" onclick="toggleDetails(${{i}})">
                        <td>${{q.timestamp ? q.timestamp.slice(0, 16).replace('T', ' ') : '-'}}</td>
                        <td class="query-text" title="${{q.full_query || ''}}">${{q.query || '-'}}</td>
                        <td class="stat-vars-cell" title="${{statVarsTitle}}">${{statVarsDisplay}}</td>
                        <td class="${{statusClass}}">${{statusSymbol}}</td>
                        <td>${{q.duration_ms ? (q.duration_ms / 1000).toFixed(1) + 's' : '-'}}</td>
                        <td>${{q.tool_count || 0}}</td>
                    </tr>
                    <tr><td colspan="6">
                        <div class="tool-details" id="details-${{i}}">
                            <strong>Session:</strong> ${{q.session_id}}<br>
                            <strong>Model:</strong> ${{q.model || 'unknown'}}<br>
                            <strong>KB Enabled:</strong> ${{q.kb_enabled ? 'Yes' : 'No'}}<br>
                            <strong>Stat Vars:</strong> ${{statVars.length > 0 ? statVars.join(', ') : 'None'}}<br>
                            <strong>Tools:</strong> ${{q.tool_calls ? q.tool_calls.map(t => t.name).join(', ') : 'None'}}
                        </div>
                    </td></tr>
                `}}).join('');
            }}

            function toggleDetails(idx) {{
                const el = document.getElementById('details-' + idx);
                el.classList.toggle('show');
            }}

            function filterQueries() {{
                const search = document.getElementById('searchBox').value.toLowerCase();
                const dateFrom = document.getElementById('dateFrom').value;
                const dateTo = document.getElementById('dateTo').value;

                if (!analyticsData) return;

                const filtered = analyticsData.recent_queries.filter(q => {{
                    // Text search filter
                    const matchesSearch = !search ||
                        (q.query && q.query.toLowerCase().includes(search)) ||
                        (q.session_id && q.session_id.toLowerCase().includes(search));

                    // Date filter
                    let matchesDate = true;
                    if (q.timestamp) {{
                        const queryDate = q.timestamp.slice(0, 10); // YYYY-MM-DD
                        if (dateFrom && queryDate < dateFrom) matchesDate = false;
                        if (dateTo && queryDate > dateTo) matchesDate = false;
                    }}

                    return matchesSearch && matchesDate;
                }});
                renderQueriesTable(filtered);
            }}

            function clearDateFilter() {{
                document.getElementById('dateFrom').value = '';
                document.getElementById('dateTo').value = '';
                filterQueries();
            }}

            // Auto-refresh
            document.getElementById('autoRefresh').addEventListener('change', function() {{
                if (this.checked) {{
                    autoRefreshInterval = setInterval(loadData, 30000);
                }} else {{
                    clearInterval(autoRefreshInterval);
                }}
            }});

            // Initial load
            loadData();
            autoRefreshInterval = setInterval(loadData, 30000);
        </script>
    </body>
    </html>
    """


def main():
    """Main entry point."""
    print("=" * 60)
    print("Data Commons MCP Proxy Server (Proxy-Only Mode)")
    print("=" * 60)
    print(f"\nExpecting MCP server at: http://localhost:{MCP_PORT}")
    print("\nMake sure you started the MCP server first:")
    print(f"  python3 -m uv tool run datacommons-mcp serve http --port {MCP_PORT}")

    # Try to connect to MCP server
    print("\nChecking MCP server connection...")
    if initialize_mcp():
        tools = get_tools()
        print(f"\nConnected! Found {len(tools)} tools:")
        for t in tools:
            print(f"  - {t.get('name')}")
    else:
        print("\nWARNING: Could not connect to MCP server")
        print("The proxy will start anyway - MCP server can be started later")

    # Start proxy
    print(f"\nStarting proxy on port {PROXY_PORT}...")
    print(f"Frontend should connect to: http://localhost:{PROXY_PORT}")
    print("\nPress Ctrl+C to stop")
    print("=" * 60)

    app.run(host="0.0.0.0", port=PROXY_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()

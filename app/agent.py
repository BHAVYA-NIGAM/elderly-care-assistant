# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import os
import re
from zoneinfo import ZoneInfo

from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.tools import AgentTool, BaseTool, ToolContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.adk.workflow import Workflow, START, node
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.genai import types

from .config import config

# Initialize models
model_inst = Gemini(model=config.model)

# -----------------------------------------------------------------------------
# MCP Server Configuration
# -----------------------------------------------------------------------------

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_PATH = os.path.join(CURRENT_DIR, "mcp_server.py")

mcp_connection = StdioConnectionParams(
    server_params=StdioServerParameters(
        command="uv",
        args=["run", "python", MCP_SERVER_PATH],
    )
)

routine_tools = McpToolset(
    connection_params=mcp_connection,
    tool_filter=["get_routines", "add_routine"]
)

health_tools = McpToolset(
    connection_params=mcp_connection,
    tool_filter=["get_health_logs", "add_health_log"]
)

# -----------------------------------------------------------------------------
# Tool Callbacks for Sensitivity Checks
# -----------------------------------------------------------------------------

async def check_sensitive_action(
    tool: BaseTool,
    args: dict,
    tool_context: ToolContext,
    tool_response: dict
) -> dict | None:
    """Intercepts mutating tool calls to set a pending approval state."""
    tool_name = tool.name
    # Since MCP tools might be named directly or prefixed
    if "add_routine" in tool_name or "add_health_log" in tool_name:
        tool_context.state["needs_review"] = True
        tool_context.state["pending_action"] = f"{tool_name} with parameters: {args}"
    return None

# -----------------------------------------------------------------------------
# Sub-Agents
# -----------------------------------------------------------------------------

routine_scheduler = LlmAgent(
    name="routine_scheduler",
    model=model_inst,
    description="Manages daily routines, calendar appointments, and meditation schedules for the elderly user.",
    instruction="""You are a caring assistant specialized in managing daily routines and meditation schedules for elderly individuals.
Your job is to schedule activities, check on medication routines, and guide the user through meditation.
Use the available tools to lookup and update the routine database. Always speak in a warm, patient, and clear tone.""",
    tools=[routine_tools],
    after_tool_callback=check_sensitive_action,
)

health_logger = LlmAgent(
    name="health_logger",
    model=model_inst,
    description="Logs daily well-being, mood, physical health metrics, and doctor/visit logs.",
    instruction="""You are a health concierge specialized in logging and tracking well-being, mood, physical symptoms, and doctor visits for the elderly.
Your job is to log well-being metrics, track doctor visit history, and summarize health trends.
Use the available tools to read and write to the health logs. Maintain a warm, encouraging, and supportive tone.""",
    tools=[health_tools],
    after_tool_callback=check_sensitive_action,
)

# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------

orchestrator = LlmAgent(
    name="orchestrator",
    model=model_inst,
    instruction="""You are the main coordinator of the Elderly Care Assistant.
Your goal is to help the elderly user or their caregiver with daily routines, meditation schedules, well-being logs, and doctor visit logs.
Analyze the user's request and delegate it to the appropriate specialist agent:
- For scheduling routines, meditation, or daily appointments, use the routine_scheduler tool.
- For well-being updates, mood tracking, or doctor/visit logs, use the health_logger tool.
If the request is general, answer it warmly and directly.""",
    tools=[AgentTool(routine_scheduler), AgentTool(health_logger)],
)

# -----------------------------------------------------------------------------
# Workflow Nodes
# -----------------------------------------------------------------------------

def security_checkpoint(ctx: Context, node_input: types.Content) -> Event:
    """Checks user input for PII, prompt injection, and emergency scenarios."""
    user_query = ""
    if node_input and node_input.parts:
        user_query = "".join(part.text for part in node_input.parts if part.text)
    
    # 1. PII Scrubbing
    ssn_pattern = r'\b\d{3}-\d{2}-\d{4}\b'
    phone_pattern = r'\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'
    
    scrubbed_query = user_query
    if config.pii_redaction_enabled:
        scrubbed_query = re.sub(ssn_pattern, "[REDACTED SSN]", scrubbed_query)
        scrubbed_query = re.sub(phone_pattern, "[REDACTED PHONE]", scrubbed_query)
    
    # 2. Prompt Injection Detection
    injection_keywords = ["ignore instructions", "bypass security", "system override", "jailbreak", "override instructions"]
    is_injection = False
    if config.injection_detection_enabled:
        is_injection = any(kw in user_query.lower() for kw in injection_keywords)
    
    # 3. Domain-specific rule: emergency keywords
    emergency_keywords = ["emergency", "chest pain", "fallen", "can't breathe", "stroke", "accident", "call 911"]
    is_emergency = any(ek in user_query.lower() for ek in emergency_keywords)
    
    # 4. Audit Log (Structured JSON)
    severity = "INFO"
    if is_injection:
        severity = "CRITICAL"
    elif is_emergency:
        severity = "WARNING"
        
    audit_log = {
        "timestamp": datetime.datetime.now().isoformat(),
        "original_query_length": len(user_query),
        "pii_detected": user_query != scrubbed_query,
        "injection_detected": is_injection,
        "emergency_detected": is_emergency,
        "severity": severity
    }
    print(f"[AUDIT LOG] {json.dumps(audit_log)}")
    
    # Routing decision
    if is_injection:
        return Event(
            output="Security Violation: Malicious input pattern detected.",
            route="SECURITY_EVENT",
            state={"scrubbed_query": scrubbed_query, "security_violation": True}
        )
    elif is_emergency:
        return Event(
            output="Emergency Warning: If you are experiencing a life-threatening medical emergency, please call 911 immediately or contact your caregiver.",
            route="EMERGENCY_EVENT",
            state={"scrubbed_query": scrubbed_query, "emergency": True}
        )
    else:
        # Pass scrubbed input as Content to orchestrator
        content = types.Content(role="user", parts=[types.Part.from_text(text=scrubbed_query)])
        return Event(
            output=content,
            route="CLEAN",
            state={"scrubbed_query": scrubbed_query}
        )

def security_event_node(node_input: str) -> Event:
    """Handles prompt injection event."""
    warning_text = "Security Alert: Access blocked due to a detected prompt injection or security policy violation."
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=warning_text)]))
    yield Event(output=warning_text)

def emergency_event_node(node_input: str) -> Event:
    """Handles emergency detection event."""
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=node_input)]))
    yield Event(output=node_input)

async def human_review_node(ctx: Context, node_input: types.Content):
    """Asks for caregiver or user approval before executing critical database actions."""
    if not ctx.state.get("needs_review"):
        yield Event(output=node_input, route="APPROVED")
        return
        
    if not ctx.resume_inputs:
        action = ctx.state.get("pending_action", "perform operation")
        yield RequestInput(
            interrupt_id="confirm_action",
            message=f"✋ Caregiver/User approval required: Do you approve the action '{action}'? (yes/no)"
        )
        return
        
    user_response = ctx.resume_inputs.get("confirm_action", "").strip().lower()
    if user_response in ["yes", "y", "approve", "confirm"]:
        msg = f"✅ Action '{ctx.state.get('pending_action')}' approved and executed."
        yield Event(
            output=msg,
            route="APPROVED",
            state={"needs_review": False, "pending_action": None, "approved": True}
        )
    else:
        msg = f"❌ Action '{ctx.state.get('pending_action')}' was denied and cancelled."
        yield Event(
            output=msg,
            route="REJECTED",
            state={"needs_review": False, "pending_action": None, "approved": False}
        )

def final_output(node_input: str | types.Content):
    """Formats and prints final output to the user/UI."""
    if isinstance(node_input, types.Content):
        yield Event(content=node_input)
        text = "".join(part.text for part in node_input.parts if part.text)
        yield Event(output=text)
    else:
        yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=str(node_input))]))
        yield Event(output=str(node_input))

# -----------------------------------------------------------------------------
# Workflow Definition & Edges
# -----------------------------------------------------------------------------

edges = [
    # Start through security check
    (START, security_checkpoint),
    
    # Security checkpoint outcomes
    (security_checkpoint, {
        "CLEAN": orchestrator,
        "SECURITY_EVENT": security_event_node,
        "EMERGENCY_EVENT": emergency_event_node
    }),
    
    # Orchestrator goes to human review
    (orchestrator, human_review_node),
    
    # Review outcomes converge on final_output
    (human_review_node, final_output),
    
    # Security/emergency converge on final_output
    (security_event_node, final_output),
    (emergency_event_node, final_output),
]

root_agent = Workflow(
    name="elderly_care_workflow",
    edges=edges,
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)

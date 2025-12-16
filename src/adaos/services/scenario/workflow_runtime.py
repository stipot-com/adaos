from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable

import anyio

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.yjs.webspace import default_webspace_id
from adaos.skills.runtime_runner import execute_tool as execute_skill_tool

_log = logging.getLogger("adaos.scenario.workflow")


def _payload(evt: Dict[str, Any]) -> Dict[str, Any]:
  """
  Event bus adapter passes the payload dict directly into handlers, so
  ``evt`` is already the payload. Keep a small helper for future changes.
  """
  return evt if isinstance(evt, dict) else {}


def _resolve_webspace_id(payload: Dict[str, Any]) -> str:
  value = payload.get("webspace_id") or payload.get("workspace_id")
  if isinstance(value, str) and value.strip():
    return value.strip()
  return default_webspace_id()


@dataclass(slots=True)
class ScenarioWorkflowRuntime:
  """
  Lightweight workflow projection for scenarios.

  Responsibilities:
    - read ``workflow`` section from scenario.json,
    - maintain current workflow state and next_actions in Yjs,
    - execute state transitions in response to actions.

  For v0.1 this runtime only updates Yjs and does not call skill tools;
  tool execution remains the responsibility of UI/skills. This keeps the
  core service simple while we validate the Prompt IDE workflow.
  """

  ctx: AgentContext

  async def sync_workflow_for_webspace(self, scenario_id: str, webspace_id: str) -> None:
    """
    Initialise or refresh workflow state for the given scenario+webspace
    based on the scenario.json ``workflow`` section.
    """
    content = scenarios_loader.read_content(scenario_id)
    wf = (content.get("workflow") or {}) if isinstance(content, dict) else {}
    if not wf:
      return

    states = wf.get("states") or {}
    if not isinstance(states, dict) or not states:
      return
    initial = wf.get("initial_state")
    if not isinstance(initial, str) or not initial:
      # fallback: first key in states
      initial = next(iter(states.keys()))

    # Compute next_actions for the current state.
    next_actions = self._actions_for_state(states, initial)

    async with async_get_ydoc(webspace_id) as ydoc:
      data_map = ydoc.get_map("data")
      with ydoc.begin_transaction() as txn:
        # For desktop Prompt IDE we keep workflow state under data.prompt.*.
        # For non-desktop/system scenarios we use data.scenarios.<id>.workflow.
        if scenario_id == "prompt_engineer_scenario":
          prompt_section = data_map.get("prompt")
          if not isinstance(prompt_section, dict):
            prompt_section = {}
          wf_obj = dict(prompt_section.get("workflow") or {})
          wf_obj["state"] = initial
          wf_obj["next_actions"] = json.loads(json.dumps(next_actions))
          prompt_section["workflow"] = wf_obj

          # Initialise Prompt IDE-specific sections for the prompt_engineer_scenario.
          status_section = dict(prompt_section.get("status") or {})
          status_section["buttons"] = self._build_status_buttons(webspace_id, states, initial)
          prompt_section["status"] = status_section
          # Files and LLM status helpers.
          prompt_section.setdefault("files", {})
          prompt_section.setdefault("llm_status", {"status": "idle", "message": "LLM: idle"})

          payload = json.loads(json.dumps(prompt_section))
          data_map.set(txn, "prompt", payload)
        else:
          scenarios_section = data_map.get("scenarios")
          if not isinstance(scenarios_section, dict):
            scenarios_section = {}
          scenario_section = dict(scenarios_section.get(scenario_id) or {})
          wf_obj = dict(scenario_section.get("workflow") or {})
          wf_obj["state"] = initial
          wf_obj["next_actions"] = json.loads(json.dumps(next_actions))
          scenario_section["workflow"] = wf_obj
          scenarios_section[scenario_id] = scenario_section
          payload = json.loads(json.dumps(scenarios_section))
          data_map.set(txn, "scenarios", payload)

  def _actions_for_state(self, states: Dict[str, Any], state_id: str) -> List[Dict[str, Any]]:
    state = states.get(state_id) or {}
    if not isinstance(state, dict):
      return []
    actions = state.get("actions") or []
    if not isinstance(actions, list):
      return []
    out: List[Dict[str, Any]] = []
    for entry in actions:
      if not isinstance(entry, dict):
        continue
      action_id = entry.get("id")
      if not isinstance(action_id, str) or not action_id:
        continue
      label = entry.get("label") or action_id
      next_state = entry.get("next_state") or state_id
      out.append(
        {
          "id": action_id,
          "label": label,
          "state": state_id,
          "next_state": next_state,
        }
      )
    return out

  async def apply_action(
    self,
    scenario_id: str,
    webspace_id: str,
    action_id: str,
    *,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
  ) -> None:
    """
    Apply a workflow action by updating current state and next_actions
    in Yjs. For v0.1 tool execution is intentionally left out.
    """
    action_id = (action_id or "").strip()
    if not action_id:
      return
    content = scenarios_loader.read_content(scenario_id)
    wf = (content.get("workflow") or {}) if isinstance(content, dict) else {}
    states = wf.get("states") or {}
    if not isinstance(states, dict) or not states:
      return

    # Determine current state from Yjs or fallback to initial_state.
    initial = wf.get("initial_state")
    if not isinstance(initial, str) or not initial:
      initial = next(iter(states.keys()))

    action_meta: Optional[Dict[str, Any]] = None
    resolved_object_type: Optional[str] = object_type
    resolved_object_id: Optional[str] = object_id

    async with async_get_ydoc(webspace_id) as ydoc:
      data_map = ydoc.get_map("data")
      with ydoc.begin_transaction() as txn:
        if scenario_id == "prompt_engineer_scenario":
          prompt_section = data_map.get("prompt")
          if not isinstance(prompt_section, dict):
            prompt_section = {}
          wf_obj = dict(prompt_section.get("workflow") or {})
          # For Prompt IDE we allow fallback to data.prompt.workflow.state.
          current_state = wf_obj.get("state") or self._read_state(ydoc) or initial
        else:
          scenarios_section = data_map.get("scenarios")
          if not isinstance(scenarios_section, dict):
            scenarios_section = {}
          scenario_section = dict(scenarios_section.get(scenario_id) or {})
          wf_obj = dict(scenario_section.get("workflow") or {})
          # For non-Prompt scenarios rely only on per-scenario workflow state
          # and initial_state; avoid leaking prompt workflow state (e.g. "tz").
          current_state = wf_obj.get("state") or initial

        if scenario_id != "greet_on_boot":
          _log.debug(
            "workflow.action.state scenario=%s webspace=%s current_state=%s action=%s",
            scenario_id,
            webspace_id,
            current_state,
            action_id,
          )

        # If object binding is not passed explicitly, try to reuse the
        # last known binding stored in the workflow projection.
        if not resolved_object_type:
          value = wf_obj.get("object_type")
          if isinstance(value, str) and value:
            resolved_object_type = value
        if not resolved_object_id:
          value = wf_obj.get("object_id")
          if isinstance(value, str) and value:
            resolved_object_id = value

        # Resolve action metadata (including optional tool and next_state).
        action_meta = self._resolve_action(states, current_state, action_id)
        if not action_meta:
          if scenario_id != "greet_on_boot":
            _log.debug(
              "workflow.action.missing_entry scenario=%s webspace=%s state=%s action=%s",
              scenario_id,
              webspace_id,
              current_state,
              action_id,
            )
          return

        next_state = action_meta.get("next_state") or current_state

        wf_obj["state"] = next_state
        wf_obj["next_actions"] = json.loads(
          json.dumps(self._actions_for_state(states, next_state))
        )
        if resolved_object_type:
          wf_obj["object_type"] = resolved_object_type
        if resolved_object_id:
          wf_obj["object_id"] = resolved_object_id

        if scenario_id == "prompt_engineer_scenario":
          prompt_section["workflow"] = wf_obj
          # Keep status bar buttons in sync for Prompt IDE scenario.
          status_section = dict(prompt_section.get("status") or {})
          status_section["buttons"] = self._build_status_buttons(
            webspace_id,
            states,
            next_state,
            wf_obj.get("object_id"),
          )
          prompt_section["status"] = status_section
          payload = json.loads(json.dumps(prompt_section))
          data_map.set(txn, "prompt", payload)
        else:
          scenario_section["workflow"] = wf_obj
          scenarios_section[scenario_id] = scenario_section
          payload = json.loads(json.dumps(scenarios_section))
          data_map.set(txn, "scenarios", payload)

    # Execute associated tool (if any) outside of the YDoc transaction.
    if action_meta is not None:
      tool_spec = action_meta.get("tool")
      if isinstance(tool_spec, str) and tool_spec.strip():
        await self._execute_action_tool(
          scenario_id,
          webspace_id,
          tool_spec.strip(),
          object_type=resolved_object_type,
          object_id=resolved_object_id,
        )

  def _read_state(self, ydoc: Any) -> Optional[str]:
    data_map = ydoc.get_map("data")
    # For Prompt IDE use data.prompt.workflow.state; for other scenarios this
    # helper is only used as a fallback and can be extended when needed.
    raw = data_map.get("prompt")
    if isinstance(raw, dict):
      wf = raw.get("workflow") or {}
      if isinstance(wf, dict):
        value = wf.get("state")
        if isinstance(value, str) and value:
          return value
    return None

  async def set_state(
    self,
    scenario_id: str,
    webspace_id: str,
    state_id: str,
    *,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
  ) -> None:
    """
    Force workflow into a specific state without executing any action.

    Used when switching projects in Prompt IDE so that the global
    workflow projection matches per-project saved state.
    """
    state_id = (state_id or "").strip()
    if not state_id:
      return
    content = scenarios_loader.read_content(scenario_id)
    wf = (content.get("workflow") or {}) if isinstance(content, dict) else {}
    states = wf.get("states") or {}
    if not isinstance(states, dict) or not states:
      return
    if state_id not in states:
      # Fallback: ignore unknown state ids.
      return

    async with async_get_ydoc(webspace_id) as ydoc:
      data_map = ydoc.get_map("data")
      with ydoc.begin_transaction() as txn:
        prompt_section = data_map.get("prompt")
        if not isinstance(prompt_section, dict):
          prompt_section = {}
        wf_obj = dict(prompt_section.get("workflow") or {})
        wf_obj["state"] = state_id
        if object_type:
          wf_obj["object_type"] = object_type
          if object_id:
            wf_obj["object_id"] = object_id
          wf_obj["next_actions"] = json.loads(
            json.dumps(self._actions_for_state(states, state_id))
          )
          prompt_section["workflow"] = wf_obj

          if scenario_id == "prompt_engineer_scenario":
            # Keep files list in Yjs for Prompt IDE so that file panel can be
            # driven by YDoc and hub-side actions can update it.
            if object_type and object_id:
              files_obj = dict(prompt_section.get("files") or {})
              files_obj.update(
                {
                  "object_type": object_type,
                  "object_id": object_id,
                  "list": self._build_files_list(object_type, object_id),
                }
              )
              prompt_section["files"] = json.loads(json.dumps(files_obj))

            # Also refresh status bar buttons when state is forced.
            status_section = dict(prompt_section.get("status") or {})
            status_section["buttons"] = self._build_status_buttons(
              webspace_id,
              states,
              state_id,
              wf_obj.get("object_id"),
            )
            prompt_section["status"] = status_section
        payload = json.loads(json.dumps(prompt_section))
        data_map.set(txn, "prompt", payload)

  def _resolve_next_state(self, states: Dict[str, Any], current_state: str, action_id: str) -> Optional[str]:
    state = states.get(current_state) or {}
    if not isinstance(state, dict):
      return None
    actions = state.get("actions") or []
    if not isinstance(actions, list):
      return None
    for entry in actions:
      if not isinstance(entry, dict):
        continue
      if entry.get("id") != action_id:
        continue
      next_state = entry.get("next_state")
      if isinstance(next_state, str) and next_state:
        return next_state
    return None

  def _resolve_action(
    self,
    states: Dict[str, Any],
    current_state: str,
    action_id: str,
  ) -> Optional[Dict[str, Any]]:
    """
    Find the full action entry for the given state+action_id, including
    optional tool and next_state fields.
    """
    state = states.get(current_state) or {}
    if not isinstance(state, dict):
      return None
    actions = state.get("actions") or []
    if not isinstance(actions, list):
      return None
    for entry in actions:
      if not isinstance(entry, dict):
        continue
      if entry.get("id") != action_id:
        continue
      return entry
    return None

  async def _execute_action_tool(
    self,
    scenario_id: str,
    webspace_id: str,
    tool_spec: str,
    *,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
  ) -> None:
    """
    Execute a skill tool declared on a workflow action (tool="skill.method").

    For now this is intentionally minimal and focuses on dev workspace
    skills, using the same runtime helper as /api/tools/call.
    """
    parts = tool_spec.split(".", 1)
    if len(parts) != 2:
      _log.warning(
        "workflow.action.tool invalid spec scenario=%s tool=%s",
        scenario_id,
        tool_spec,
      )
      return
    skill_name, tool_name = parts[0].strip(), parts[1].strip()
    if not skill_name or not tool_name:
      return

    ctx = self.ctx
    # Resolve dev skills root and skill directory. If there is no dev
    # copy for the requested skill, fall back to the main workspace
    # skills directory so that system/workspace skills can be used in
    # workflows as well.
    dev_root = ctx.paths.dev_skills_dir()
    dev_root = dev_root() if callable(dev_root) else dev_root
    skill_dir = Path(dev_root) / skill_name
    if not skill_dir.exists():
      ws_root = ctx.paths.skills_dir()
      ws_root = ws_root() if callable(ws_root) else ws_root
      candidate = Path(ws_root) / skill_name
      if candidate.exists():
        skill_dir = candidate

    payload: Dict[str, Any] = {"webspace_id": webspace_id}
    if object_type:
      payload["object_type"] = object_type
    if object_id:
      payload["object_id"] = object_id

    if scenario_id != "greet_on_boot":
      _log.debug(
        "workflow.action.tool scenario=%s webspace=%s skill=%s tool=%s",
        scenario_id,
        webspace_id,
        skill_name,
        tool_name,
      )

    def _call_tool() -> Any:
      return execute_skill_tool(
        skill_dir,
        module=None,
        attr=tool_name,
        payload=payload,
        extra_paths=None,
      )

    # For Prompt IDE tz_execute, reflect LLM status in Yjs.
    is_prompt_ts = skill_name == "prompt_engineer_skill" and tool_name == "tz_execute"
    if is_prompt_ts:
      await self._set_llm_status(webspace_id, "operate", "LLM: operate")

    try:
      result: Any = await anyio.to_thread.run_sync(_call_tool)
    except Exception as exc:  # pragma: no cover - defensive
      _log.warning(
        "workflow.action.tool failed scenario=%s webspace=%s skill=%s tool=%s error=%s",
        scenario_id,
        webspace_id,
        skill_name,
        tool_name,
        exc,
        exc_info=True,
      )
      if is_prompt_ts:
        await self._set_llm_status(
          webspace_id,
          "error",
          f"LLM: error ({exc})",
        )
      return

    # Project LLM artifacts for the TS workflow into Yjs so that the IDE
    # can render them from the live snapshot instead of reading files.
    if is_prompt_ts:
      await self._update_llm_artifacts_from_tz_execute(
        webspace_id,
        scenario_id,
        object_type=object_type,
        object_id=object_id,
        result=result,
      )

  async def _update_llm_artifacts_from_tz_execute(
    self,
    webspace_id: str,
    scenario_id: str,
    *,
    object_type: Optional[str],
    object_id: Optional[str],
    result: Any,
  ) -> None:
    """
    Update data/prompt/llm_artifacts in Yjs from tz_execute result.

    Expected result payload:
      { ok: bool, object_type, object_id, output_text: str, ... }
    """
    if not isinstance(result, dict):
      _log.debug(
        "workflow.llm_artifacts.skip webspace=%s scenario=%s reason=non_dict_result",
        webspace_id,
        scenario_id,
      )
      return
    output_text = result.get("output_text")
    if not isinstance(output_text, str) or not output_text.strip():
      _log.debug(
        "workflow.llm_artifacts.skip webspace=%s scenario=%s reason=empty_output",
        webspace_id,
        scenario_id,
      )
      return

    ts = datetime.now(timezone.utc).isoformat()
    _log.info(
      "workflow.llm_artifacts.update webspace=%s scenario=%s object_type=%s object_id=%s length=%d",
      webspace_id,
      scenario_id,
      object_type,
      object_id,
      len(output_text),
    )

    output_path = result.get("output_path")

    async with async_get_ydoc(webspace_id) as ydoc:
      data_map = ydoc.get_map("data")
      with ydoc.begin_transaction() as txn:
        prompt_section = data_map.get("prompt")
        if not isinstance(prompt_section, dict):
          prompt_section = {}
        llm_obj = dict(prompt_section.get("llm_artifacts") or {})
        items = llm_obj.get("items") or []
        if not isinstance(items, list):
          items = []

        entry = {
          "id": "ts_draft",
          "kind": "ts_draft",
          "scenario_id": scenario_id,
          "object_type": object_type,
          "object_id": object_id,
          "title": "TS detailed implementation draft",
          "content": output_text,
          "updated_at": ts,
        }
        # Replace or append ts_draft entry.
        filtered: List[Dict[str, Any]] = []
        for it in items:
          if isinstance(it, dict) and it.get("id") != "ts_draft":
            filtered.append(it)
        filtered.append(entry)

        llm_obj["items"] = filtered
        prompt_section["llm_artifacts"] = json.loads(json.dumps(llm_obj))

        # Also expose a convenience selected file path for the IDE so
        # that it can focus on the latest TS draft artifact.
        files_obj = dict(prompt_section.get("files") or {})
        if isinstance(output_path, str) and output_path:
          files_obj["selected"] = output_path
          # Refresh file list for the current project so that newly
          # created artifacts (such as ts_draft.md) appear in the Files
          # panel without requiring a manual snapshot.
          if object_type and object_id:
            files_obj["object_type"] = object_type
            files_obj["object_id"] = object_id
            files_obj["list"] = self._build_files_list(object_type, object_id)
        prompt_section["files"] = json.loads(json.dumps(files_obj))

        # Update LLM status snapshot with last request/response for debugging.
        status_obj = dict(prompt_section.get("llm_status") or {})
        status_obj["status"] = "idle"
        status_obj["message"] = "LLM: idle"
        if isinstance(result, dict):
          if "request_prompt" in result:
            status_obj["last_request"] = result.get("request_prompt")
          if "raw_response" in result:
            status_obj["last_response"] = result.get("raw_response")
        prompt_section["llm_status"] = json.loads(json.dumps(status_obj))

        payload = json.loads(json.dumps(prompt_section))
        data_map.set(txn, "prompt", payload)

  async def _set_llm_status(
    self,
    webspace_id: str,
    status: str,
    message: str,
  ) -> None:
    """
    Update LLM status snapshot under data/prompt/llm_status and refresh
    the dedicated status bar button (llm-status) title.
    """
    async with async_get_ydoc(webspace_id) as ydoc:
      data_map = ydoc.get_map("data")
      with ydoc.begin_transaction() as txn:
        prompt_section = data_map.get("prompt")
        if not isinstance(prompt_section, dict):
          prompt_section = {}

        status_obj = dict(prompt_section.get("llm_status") or {})
        status_obj["status"] = status
        status_obj["message"] = message
        prompt_section["llm_status"] = json.loads(json.dumps(status_obj))

        status_bar = dict(prompt_section.get("status") or {})
        buttons = list(status_bar.get("buttons") or [])
        updated_buttons: List[Dict[str, Any]] = []
        for btn in buttons:
          if isinstance(btn, dict) and btn.get("id") == "llm-status":
            btn = dict(btn)
            btn["title"] = message
          updated_buttons.append(btn)
        status_bar["buttons"] = updated_buttons
        prompt_section["status"] = json.loads(json.dumps(status_bar))

        payload = json.loads(json.dumps(prompt_section))
        data_map.set(txn, "prompt", payload)

  def _project_root(self, object_type: Optional[str], object_id: Optional[str]) -> Optional[Path]:
    kind = (object_type or "").strip().lower()
    if not object_id or not kind:
      return None
    ctx = self.ctx
    if kind == "skill":
      base = ctx.paths.dev_skills_dir()
    elif kind == "scenario":
      base = ctx.paths.dev_scenarios_dir()
    else:
      return None
    base = base() if callable(base) else base
    root = (Path(base) / str(object_id)).resolve()
    return root

  def _build_files_list(self, object_type: str, object_id: str) -> List[Dict[str, Any]]:
    """
    Build a flat file listing for the prompt project rooted at dev skills/scenarios.
    """
    root = self._project_root(object_type, object_id)
    if root is None or not root.exists():
      return []

    exts = {".py", ".json", ".yml", ".yaml", ".md"}
    items: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
      if not path.is_file():
        continue
      if path.suffix.lower() not in exts:
        continue
      rel = path.relative_to(root).as_posix()
      items.append(
        {
          "id": rel,
          "label": rel,
          "path": rel,
          "kind": "file",
          "object_type": object_type,
          "object_id": object_id,
        }
      )
    return items

  def _build_status_buttons(
    self,
    webspace_id: str,
    states: Dict[str, Any],
    state_id: str,
    object_id: Optional[str] = None,
  ) -> List[Dict[str, Any]]:
    """
    Construct status bar buttons for Prompt IDE.

    Order (left-to-right):
      - Project name (opens project details),
      - Workflow stage (human-readable label from scenario.workflow.states),
      - LLM status (opens last request/response),
      - LLM profile (model selection),
      - Dev webspace id.
    """
    state_key = (state_id or "").strip()
    state_meta = states.get(state_key) or {}
    if not isinstance(state_meta, dict):
      state_meta = {}
    stage_label = state_meta.get("label")
    if not isinstance(stage_label, str) or not stage_label.strip():
      stage_label = state_key or "workflow"

    project_label = str(object_id) if object_id else "project: none"

    return [
      {
        "id": "current-object",
        "label": project_label,
        "action": {"openModal": "project_meta_modal"},
      },
      {
        "id": "workflow-stage",
        "label": stage_label,
      },
      {
        "id": "llm-status",
        "label": "LLM",
        "action": {"openModal": "llm_status_modal"},
      },
      {
        "id": "llm-profile",
        "label": "LLM profile",
        "action": {"openModal": "llm_profile_modal"},
      },
      {
        "id": "dev-webspace",
        "label": f"dev webspace: {webspace_id}",
      },
    ]


@subscribe("scenario.workflow.action")
async def _on_workflow_action(evt: Dict[str, Any]) -> None:
  """
  Handle workflow action requests coming from IO layers (web, chat, voice).

  Payload:
    - scenario_id: scenario identifier (required)
    - action_id: workflow action identifier (required)
    - object_type: optional project kind for tools (skill|scenario)
    - object_id: optional project identifier for tools
    - webspace_id / workspace_id: optional, defaults to default webspace.
  """
  payload = _payload(evt)
  scenario_id = str(payload.get("scenario_id") or "").strip()
  action_id = str(payload.get("action_id") or "").strip()
  if not scenario_id or not action_id:
    return
  webspace_id = _resolve_webspace_id(payload)
  object_type = payload.get("object_type")
  object_id = payload.get("object_id")
  ctx = get_ctx()
  runtime = ScenarioWorkflowRuntime(ctx)
  _log.debug(
    "workflow.action scenario=%s webspace=%s action=%s object_type=%s object_id=%s",
    scenario_id,
    webspace_id,
    action_id,
    object_type,
    object_id,
  )
  try:
    await runtime.apply_action(
      scenario_id,
      webspace_id,
      action_id,
      object_type=str(object_type) if object_type else None,
      object_id=str(object_id) if object_id else None,
    )
  except Exception as exc:  # pragma: no cover - defensive
    _log.warning(
      "workflow.action failed scenario=%s webspace=%s action=%s error=%s",
      scenario_id,
      webspace_id,
      action_id,
      exc,
      exc_info=True,
    )


@subscribe("scenario.workflow.set_state")
async def _on_workflow_set_state(evt: Dict[str, Any]) -> None:
  """
  Force workflow state (without executing tools).

  Payload:
    - scenario_id: scenario identifier (required)
    - state: workflow state id (required)
    - object_type: optional project kind for tools (skill|scenario)
    - object_id: optional project identifier for tools
    - webspace_id / workspace_id: optional, defaults to default webspace.
  """
  payload = _payload(evt)
  scenario_id = str(payload.get("scenario_id") or "").strip()
  state_id = str(payload.get("state") or "").strip()
  if not scenario_id or not state_id:
    return
  webspace_id = _resolve_webspace_id(payload)
  object_type = payload.get("object_type")
  object_id = payload.get("object_id")
  ctx = get_ctx()
  runtime = ScenarioWorkflowRuntime(ctx)
  _log.info(
    "workflow.set_state scenario=%s webspace=%s state=%s object_type=%s object_id=%s",
    scenario_id,
    webspace_id,
    state_id,
    object_type,
    object_id,
  )
  try:
    await runtime.set_state(
      scenario_id,
      webspace_id,
      state_id,
      object_type=str(object_type) if object_type else None,
      object_id=str(object_id) if object_id else None,
    )
  except Exception as exc:  # pragma: no cover - defensive
    _log.warning(
      "workflow.set_state failed scenario=%s webspace=%s state=%s error=%s",
      scenario_id,
      webspace_id,
      state_id,
      exc,
      exc_info=True,
    )

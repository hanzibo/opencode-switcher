import json
from typing import Optional
from gi.repository import GLib
import tool_registry
from ai_text_utils import _strip_ai_markup
from event_types import StreamEventType, ToolCallData, tool_call_to_dict
from llm_client import _LLMHttpError

# lazily initialized from AISettingsStore
_MAX_TOOL_ITERATIONS: Optional[int] = None

def _get_max_tool_iterations() -> int:
    global _MAX_TOOL_ITERATIONS
    if _MAX_TOOL_ITERATIONS is None:
        from clipboard_store import AISettingsStore
        _MAX_TOOL_ITERATIONS = AISettingsStore().max_tool_iterations
    return _MAX_TOOL_ITERATIONS

def _clean_messages_for_llm(messages: list) -> list:
    cleaned = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and isinstance(content, str):
            cleaned_content = _strip_ai_markup(content)
            
            msg_copy = dict(msg)
            msg_copy["content"] = cleaned_content
            cleaned.append(msg_copy)
        else:
            cleaned.append(msg)
    return cleaned

def run_llm_react_loop(
    llm_client,
    base_url: str,
    api_key: str,
    model_name: str,
    messages: list,
    req_id: int,
    temperature: float,
    max_tokens: int,
    top_p: float,
    cancel_event,
    get_current_request_id_fn,
    append_message_fn,
    append_html_to_webview_fn,
    handle_ask_user_question_fn,
    on_llm_api_finished_fn,
    finalize_after_tool_loop_fn,
    set_tool_iteration_fn,
    reset_iteration_state_fn,
    set_reasoning_text_fn=None,
    set_assistant_text_fn=None,
    on_token_delta_fn=None,
    switch_to_html_mode_fn=None,
    conv_id: str = None,
    extra_system_messages: Optional[list] = None,
):
    set_tool_iteration_fn(0)
    tool_registry.set_current_conversation_id(conv_id)
    try:
        iteration = 0
        max_iter = _get_max_tool_iterations()
        while iteration < max_iter:
            # Stop before next iteration if user cancelled (pressed pause/stop)
            if cancel_event and cancel_event.is_set():
                break

            # Check for completed background sub-agents before each LLM call
            bg_info = tool_registry.check_background_subagents()
            if bg_info:
                messages.append({
                    "role": "system",
                    "content": f"[Background sub-agent completed]\n{bg_info}"
                })

            should_continue = _perform_llm_call(
                llm_client=llm_client,
                base_url=base_url,
                api_key=api_key,
                model_name=model_name,
                messages=messages,
                req_id=req_id,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                cancel_event=cancel_event,
                get_current_request_id_fn=get_current_request_id_fn,
                append_message_fn=append_message_fn,
                append_html_to_webview_fn=append_html_to_webview_fn,
                handle_ask_user_question_fn=handle_ask_user_question_fn,
                on_llm_api_finished_fn=on_llm_api_finished_fn,
                finalize_after_tool_loop_fn=finalize_after_tool_loop_fn,
                reset_iteration_state_fn=reset_iteration_state_fn,
                iteration=iteration,
                set_reasoning_text_fn=set_reasoning_text_fn,
                set_assistant_text_fn=set_assistant_text_fn,
                on_token_delta_fn=on_token_delta_fn,
                switch_to_html_mode_fn=switch_to_html_mode_fn,
                extra_system_messages=extra_system_messages,
            )
            if not should_continue:
                break
            iteration += 1
            set_tool_iteration_fn(iteration)
    finally:
        tool_registry.set_current_conversation_id(None)

def _perform_llm_call(
    llm_client,
    base_url: str,
    api_key: str,
    model_name: str,
    messages: list,
    req_id: int,
    temperature: float,
    max_tokens: int,
    top_p: float,
    cancel_event,
    get_current_request_id_fn,
    append_message_fn,
    append_html_to_webview_fn,
    handle_ask_user_question_fn,
    on_llm_api_finished_fn,
    finalize_after_tool_loop_fn,
    reset_iteration_state_fn,
    iteration: int,
    set_reasoning_text_fn=None,
    set_assistant_text_fn=None,
    on_token_delta_fn=None,
    switch_to_html_mode_fn=None,
    extra_system_messages: Optional[list] = None,
) -> bool:
    assistant_text = ""
    reasoning_text = ""
    tool_calls_found = []

    reset_iteration_state_fn()

    try:
        cleaned_msgs = _clean_messages_for_llm(messages)
        for event in llm_client.stream_chat_completion(
            base_url, api_key, model_name, cleaned_msgs,
            timeout=30, cancel_event=cancel_event,
            temperature=temperature, max_tokens=max_tokens, top_p=top_p,
            tools=tool_registry.TOOL_DEFINITIONS,
            tool_choice=tool_registry.TOOL_CHOICE_AUTO,
            extra_system_messages=extra_system_messages,
        ):
            if get_current_request_id_fn() != req_id:
                return False

            if event.type == StreamEventType.TOOL_CALLS:
                if event.tool_calls:
                    if switch_to_html_mode_fn is not None:
                        GLib.idle_add(switch_to_html_mode_fn, req_id)
                    tool_calls_found.extend(event.tool_calls)
                continue

            if event.type == StreamEventType.REASONING_DELTA:
                if event.reasoning_delta:
                    reasoning_text += event.reasoning_delta
                    if set_reasoning_text_fn is not None:
                        set_reasoning_text_fn(reasoning_text)
                continue

            if event.type == StreamEventType.TEXT_DELTA:
                if event.text_delta:
                    if on_token_delta_fn is not None:
                        on_token_delta_fn(event.text_delta)
                    assistant_text += event.text_delta
                    if set_assistant_text_fn is not None:
                        set_assistant_text_fn(assistant_text)
                continue

            if event.type == StreamEventType.STREAM_END:
                break


        if tool_calls_found:
            tool_call_msg = {
                "role": "assistant",
                "content": assistant_text,
                "tool_calls": [tool_call_to_dict(tc) for tc in tool_calls_found],
            }
            if reasoning_text:
                tool_call_msg["reasoning_content"] = reasoning_text
            append_message_fn(tool_call_msg)

            for tc_idx, tc in enumerate(tool_calls_found):
                if get_current_request_id_fn() != req_id:
                    return False
                tc_name = tc.name
                if tc_name == "ask_user_question":
                    result = handle_ask_user_question_fn(tool_call_to_dict(tc))
                else:
                    result = tool_registry.execute_tool_call(tool_call_to_dict(tc), cancel_event=cancel_event)
                if get_current_request_id_fn() != req_id:
                    return False
                if cancel_event and cancel_event.is_set():
                    # Append result for the cancelled tool itself
                    append_message_fn({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc_name,
                        "content": result,
                    })
                    # Then append cancelled results for remaining unexecuted tools
                    for remaining_tc in tool_calls_found[tc_idx + 1:]:
                        append_message_fn({
                            "role": "tool",
                            "tool_call_id": remaining_tc.id,
                            "name": remaining_tc.name,
                            "content": tool_registry.TOOL_CANCELLED,
                        })
                    return False
                append_message_fn({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc_name,
                    "content": result,
                })

            if cancel_event and cancel_event.is_set():
                return False

            max_iter = _get_max_tool_iterations()
            if iteration + 1 >= max_iter:
                append_message_fn({
                    "role": "assistant",
                    "content": f"⚠️ 已达到最大迭代次数（{max_iter}），请简化请求或重试。"
                })
                GLib.idle_add(finalize_after_tool_loop_fn, req_id)
                return False

            return True
        else:
            GLib.idle_add(on_llm_api_finished_fn, req_id)
            return False

    except _LLMHttpError:
        GLib.idle_add(on_llm_api_finished_fn, req_id)
        return False
    except Exception:
        GLib.idle_add(on_llm_api_finished_fn, req_id)
        return False

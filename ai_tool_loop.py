import json
from gi.repository import GLib
import tool_registry
from ai_text_utils import _strip_ai_markup
from llm_client import _LLMHttpError

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

MAX_TOOL_ITERATIONS = 25

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
    stream_lock,
    stream_queue,
    get_current_request_id_fn,
    append_message_fn,
    append_html_to_webview_fn,
    flush_stream_queue_fn,
    append_to_stream_queue_fn,
    handle_ask_user_question_fn,
    on_llm_api_finished_fn,
    finalize_after_tool_loop_fn,
    set_tool_iteration_fn,
    reset_iteration_state_fn,
    set_reasoning_text_fn=None,
    set_assistant_text_fn=None,
    conv_id: str = None,
):
    set_tool_iteration_fn(0)
    tool_registry.set_current_conversation_id(conv_id)
    try:
        iteration = 0
        while iteration < MAX_TOOL_ITERATIONS:
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
                stream_lock=stream_lock,
                stream_queue=stream_queue,
                get_current_request_id_fn=get_current_request_id_fn,
                append_message_fn=append_message_fn,
                append_html_to_webview_fn=append_html_to_webview_fn,
                flush_stream_queue_fn=flush_stream_queue_fn,
                append_to_stream_queue_fn=append_to_stream_queue_fn,
                handle_ask_user_question_fn=handle_ask_user_question_fn,
                on_llm_api_finished_fn=on_llm_api_finished_fn,
                finalize_after_tool_loop_fn=finalize_after_tool_loop_fn,
                reset_iteration_state_fn=reset_iteration_state_fn,
                iteration=iteration,
                set_reasoning_text_fn=set_reasoning_text_fn,
                set_assistant_text_fn=set_assistant_text_fn,
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
    stream_lock,
    stream_queue,
    get_current_request_id_fn,
    append_message_fn,
    append_html_to_webview_fn,
    flush_stream_queue_fn,
    append_to_stream_queue_fn,
    handle_ask_user_question_fn,
    on_llm_api_finished_fn,
    finalize_after_tool_loop_fn,
    reset_iteration_state_fn,
    iteration: int,
    set_reasoning_text_fn=None,
    set_assistant_text_fn=None,
) -> bool:
    assistant_text = ""
    reasoning_text = ""
    tool_calls_found = []

    reset_iteration_state_fn()

    try:
        cleaned_msgs = _clean_messages_for_llm(messages)
        for delta in llm_client.stream_chat_completion(
            base_url, api_key, model_name, cleaned_msgs,
            timeout=30, cancel_event=cancel_event,
            temperature=temperature, max_tokens=max_tokens, top_p=top_p,
            tools=tool_registry.TOOL_DEFINITIONS,
            tool_choice=tool_registry.TOOL_CHOICE_AUTO,
        ):
            if get_current_request_id_fn() != req_id:
                return False

            tc_delta = delta.get("tool_calls")
            if tc_delta:
                tool_calls_found.extend(tc_delta)
                continue

            reasoning = delta.get("reasoning_content")
            content = delta.get("content")

            if reasoning:
                reasoning_text += reasoning
                if set_reasoning_text_fn is not None:
                    set_reasoning_text_fn(reasoning_text)
                with stream_lock:
                    append_to_stream_queue_fn(reasoning)
            elif content:
                assistant_text += content
                if set_assistant_text_fn is not None:
                    set_assistant_text_fn(assistant_text)
                with stream_lock:
                    append_to_stream_queue_fn(content)


        if tool_calls_found:
            tool_call_msg = {
                "role": "assistant",
                "content": assistant_text,
                "tool_calls": tool_calls_found,
            }
            if reasoning_text:
                tool_call_msg["reasoning_content"] = reasoning_text
            append_message_fn(tool_call_msg)

            flush_stream_queue_fn()

            for tc in tool_calls_found:
                if get_current_request_id_fn() != req_id:
                    return False
                tc_name = tc.get("function", {}).get("name", "")
                if tc_name == "ask_user_question":
                    result = handle_ask_user_question_fn(tc)
                else:
                    result = tool_registry.execute_tool_call(tc, cancel_event=cancel_event)
                if get_current_request_id_fn() != req_id:
                    return False
                append_message_fn({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": tc_name,
                    "content": result,
                })
                if cancel_event and cancel_event.is_set():
                    return False

            if cancel_event and cancel_event.is_set():
                return False

            if iteration + 1 >= MAX_TOOL_ITERATIONS:
                append_message_fn({
                    "role": "assistant",
                    "content": f"⚠️ 已达到最大工具调用次数（{MAX_TOOL_ITERATIONS}），请简化请求或重试。"
                })
                GLib.idle_add(finalize_after_tool_loop_fn, req_id)
                return False

            return True
        else:
            GLib.idle_add(on_llm_api_finished_fn, req_id)
            return False

    except _LLMHttpError as e:
        with stream_lock:
            append_to_stream_queue_fn(f"\n\n❌ [请求失败]:\n{e}")
        GLib.idle_add(on_llm_api_finished_fn, req_id)
        return False
    except Exception as e:
        with stream_lock:
            append_to_stream_queue_fn(f"\n\n❌ [内部错误]:\n{e}")
        GLib.idle_add(on_llm_api_finished_fn, req_id)
        return False

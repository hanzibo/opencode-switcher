import re
import json
from gi.repository import GLib
import tool_registry
from llm_client import _LLMHttpError

def _clean_messages_for_llm(messages: list) -> list:
    cleaned = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and isinstance(content, str):
            # Strip details thinking blocks
            cleaned_content = re.sub(
                r'<details class=["\']thinking-details["\'].*?</details>\n*',
                "", content, flags=re.DOTALL
            )
            # Strip header divs
            cleaned_content = re.sub(
                r'<div class=["\'](?:assistant|thinking|answer)-header["\'].*?</div>\n*',
                "", cleaned_content, flags=re.DOTALL
            )
            # Strip details summary and div tags
            cleaned_content = re.sub(
                r'</?details.*?>|</?summary.*?>|</?div.*?>',
                "", cleaned_content
            )
            cleaned_content = cleaned_content.strip()
            
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
):
    set_tool_iteration_fn(0)
    iteration = 0
    while iteration < MAX_TOOL_ITERATIONS:
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
        )
        if not should_continue:
            break
        iteration += 1
        set_tool_iteration_fn(iteration)

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
) -> bool:
    has_thinking = False
    thinking_header_added = False
    response_header_added = False
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
                if not thinking_header_added:
                    with stream_lock:
                        append_to_stream_queue_fn('<details class="thinking-details" open><summary class="thinking-summary">💭 Thinking Process</summary><div class="thinking-content">')
                    thinking_header_added = True
                with stream_lock:
                    append_to_stream_queue_fn(reasoning)
                reasoning_text += reasoning
                if set_reasoning_text_fn is not None:
                    set_reasoning_text_fn(reasoning_text)
                has_thinking = True
            elif content:
                if not response_header_added:
                    response_header_added = True
                    if has_thinking:
                        with stream_lock:
                            append_to_stream_queue_fn('</div></details>\n\n<div class="answer-header">💡 Answer:</div>\n')
                    else:
                        with stream_lock:
                            append_to_stream_queue_fn('\n\n<div class="assistant-header">🤖 Assistant:</div>\n')
                with stream_lock:
                    append_to_stream_queue_fn(content)
                assistant_text += content

        if thinking_header_added and not response_header_added:
            with stream_lock:
                append_to_stream_queue_fn('</div></details>\n\n')

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

            tc_html = tool_registry.format_tool_calls_for_display(tool_calls_found)
            GLib.idle_add(append_html_to_webview_fn, tc_html)

            for tc in tool_calls_found:
                if get_current_request_id_fn() != req_id:
                    return False
                tc_name = tc.get("function", {}).get("name", "")
                if tc_name == "ask_user_question":
                    result = handle_ask_user_question_fn(tc)
                else:
                    result = tool_registry.execute_tool_call(tc)
                if get_current_request_id_fn() != req_id:
                    return False
                append_message_fn({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": tc_name,
                    "content": result,
                })
                result_html = tool_registry.format_tool_result_for_display(tc_name, result)
                GLib.idle_add(append_html_to_webview_fn, result_html)

            if iteration + 1 >= MAX_TOOL_ITERATIONS:
                err_msg = (
                    f'\n\n<div class="tool-result"><b>⚠️ 已达到最大工具调用次数'
                    f'（{MAX_TOOL_ITERATIONS}），请简化请求或重试。</b></div>\n\n'
                )
                GLib.idle_add(append_html_to_webview_fn, err_msg)
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

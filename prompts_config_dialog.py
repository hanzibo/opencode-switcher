import os
import re
import json
import base64
from uuid import uuid4
from copy import deepcopy
from gi.repository import Gtk, Gdk
from typing import Optional, Callable

from clipboard_store import (
    CustomPrompt,
    CustomPromptsStore,
    LLMSettingsStore,
    LLMModelConfig,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TOP_P,
)


class PromptsConfigDialog:
    def __init__(self, parent_window, custom_prompts_store: CustomPromptsStore,
                 llm_settings_store: LLMSettingsStore,
                 on_dialog_shown: Optional[Callable[[], None]],
                 on_dialog_hidden: Optional[Callable[[], None]]):
        self.parent_window = parent_window
        self.custom_prompts_store = custom_prompts_store
        self.llm_settings_store = llm_settings_store
        self.on_dialog_shown = on_dialog_shown
        self.on_dialog_hidden = on_dialog_hidden

        self._active_model_idx = -1
        self._dialog_active_idx = -1
        self._editing_global_settings = False
        self._updating_model_ui = False
        self._model_row_selected_handler_id = None

        self.build_ui()

    def build_ui(self):
        prompts = self.custom_prompts_store.get_all()
        self._dialog_active_idx = 0 if prompts else -1
        tab_buttons = {}

        dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        dialog.get_style_context().add_class("custom-dialog")
        dialog.set_title("Prompts Config")
        dialog.set_modal(True)
        dialog.set_default_size(750, 550)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        dialog.set_resizable(True)
        dialog.set_transient_for(self.parent_window)

        # Track LLM settings edit state
        self._editing_global_settings = False

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        dialog.add(vbox)

        title_label = Gtk.Label.new("Prompts Config")
        title_label.set_xalign(0)
        title_label.set_margin_start(12)
        title_label.set_margin_top(8)
        title_label.set_margin_bottom(8)
        vbox.pack_start(title_label, False, False, 0)

        sep1 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep1, False, False, 0)

        # Tab bar (scrolled box)
        top_bar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        top_bar.set_margin_start(12)
        top_bar.set_margin_end(12)
        top_bar.set_margin_top(8)
        top_bar.set_margin_bottom(8)

        tab_scrolled = Gtk.ScrolledWindow.new()
        tab_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        tab_scrolled.set_hexpand(True)

        tab_bar_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        tab_scrolled.add(tab_bar_box)
        top_bar.pack_start(tab_scrolled, True, True, 0)

        add_btn = Gtk.Button.new_with_label("➕")
        add_btn.set_tooltip_text("Add new prompt")
        top_bar.pack_start(add_btn, False, False, 0)

        # Global LLM API Config Button
        settings_btn = Gtk.Button.new_with_label("⚙️ API Settings")
        settings_btn.set_tooltip_text("Configure Global LLM API credentials")
        top_bar.pack_start(settings_btn, False, False, 0)

        vbox.pack_start(top_bar, False, False, 0)

        # Content edit area
        mid_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)
        mid_vbox.set_margin_start(12)
        mid_vbox.set_margin_end(12)
        mid_vbox.set_margin_top(8)
        mid_vbox.set_margin_bottom(8)

        # Container for editing prompts
        prompt_edit_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

        name_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        name_label = Gtk.Label.new("菜单显示名称:")
        name_label.set_xalign(0)
        name_entry = Gtk.Entry.new()
        name_entry.set_hexpand(True)
        name_hbox.pack_start(name_label, False, False, 0)
        name_hbox.pack_start(name_entry, True, True, 0)
        prompt_edit_box.pack_start(name_hbox, False, False, 0)

        prompt_label_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        prompt_label = Gtk.Label.new("追加提示词:")
        prompt_label.set_xalign(0)
        prompt_label_hbox.pack_start(prompt_label, True, True, 0)

        insert_btn = Gtk.Button.new_with_label("+ ${&}")
        insert_btn.set_tooltip_text("插入剪切板内容占位符")
        insert_btn.get_style_context().add_class("flat")

        def on_insert_clicked(_btn):
            buffer = prompt_textview.get_buffer()
            buffer.insert_at_cursor("${&}")
            prompt_textview.grab_focus()

        insert_btn.connect("clicked", on_insert_clicked)
        prompt_label_hbox.pack_end(insert_btn, False, False, 0)
        prompt_edit_box.pack_start(prompt_label_hbox, False, False, 0)

        prompt_scrolled = Gtk.ScrolledWindow.new()
        prompt_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        prompt_scrolled.set_vexpand(True)

        prompt_textview = Gtk.TextView.new()
        prompt_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        prompt_scrolled.add(prompt_textview)
        prompt_edit_box.pack_start(prompt_scrolled, True, True, 0)

        # Executing mode toggle buttons
        mode_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
        mode_label = Gtk.Label.new("执行模式:")
        mode_label.set_xalign(0)
        mode_hbox.pack_start(mode_label, False, False, 0)

        mode_web_radio = Gtk.RadioButton.new_with_label(None, "Web 搜索 (Google)")
        mode_api_radio = Gtk.RadioButton.new_with_label_from_widget(mode_web_radio, "API 询问 (原生 API)")
        mode_hbox.pack_start(mode_web_radio, False, False, 0)
        mode_hbox.pack_start(mode_api_radio, False, False, 0)
        prompt_edit_box.pack_start(mode_hbox, False, False, 4)

        # Backend Model selection
        model_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
        model_lbl = Gtk.Label.new("后端模型:")
        model_lbl.set_xalign(0)
        model_combo = Gtk.ComboBoxText.new()
        model_hbox.pack_start(model_lbl, False, False, 0)
        model_hbox.pack_start(model_combo, True, True, 0)
        prompt_edit_box.pack_start(model_hbox, False, False, 4)

        def on_mode_toggled(widget):
            model_combo.set_sensitive(mode_api_radio.get_active())
        mode_api_radio.connect("toggled", on_mode_toggled)
        mode_web_radio.connect("toggled", on_mode_toggled)

        # Checkboxes for categories
        applicability_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
        applicability_hbox.set_margin_top(4)
        applicability_hbox.set_margin_bottom(4)

        app_label = Gtk.Label.new("适用类别:")
        app_label.set_xalign(0)
        applicability_hbox.pack_start(app_label, False, False, 0)

        select_all_check = Gtk.CheckButton.new_with_label("全选")
        text_check = Gtk.CheckButton.new_with_label("文本")
        link_check = Gtk.CheckButton.new_with_label("链接")
        code_check = Gtk.CheckButton.new_with_label("代码")

        applicability_hbox.pack_start(select_all_check, False, False, 0)
        applicability_hbox.pack_start(text_check, False, False, 0)
        applicability_hbox.pack_start(link_check, False, False, 0)
        applicability_hbox.pack_start(code_check, False, False, 0)

        prompt_edit_box.pack_start(applicability_hbox, False, False, 0)
        mid_vbox.pack_start(prompt_edit_box, True, True, 0)

        # Container for global LLM API credentials configuration (Model Pool Management)
        llm_edit_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)

        # Local model list copy state
        local_models = []
        self._active_model_idx = -1
        self._updating_model_ui = False

        # Left side: Models List
        vbox_left = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)
        vbox_left.set_size_request(160, -1)

        model_list_scrolled = Gtk.ScrolledWindow.new()
        model_list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        model_list_scrolled.set_shadow_type(Gtk.ShadowType.IN)
        model_list_scrolled.set_vexpand(True)

        model_list_box = Gtk.ListBox.new()
        model_list_scrolled.add(model_list_box)
        vbox_left.pack_start(model_list_scrolled, True, True, 0)

        btn_add_model = Gtk.Button.new_with_label("➕ 添加模型")
        vbox_left.pack_start(btn_add_model, False, False, 0)

        llm_edit_box.pack_start(vbox_left, False, False, 0)

        # Separator
        model_sep = Gtk.Separator.new(Gtk.Orientation.VERTICAL)
        llm_edit_box.pack_start(model_sep, False, False, 6)

        # Right side: Form Fields
        vbox_right = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox_right.set_hexpand(True)

        llm_title = Gtk.Label.new()
        llm_title.set_markup("<b>模型参数配置 (OpenAI 兼容格式)</b>")
        llm_title.set_xalign(0)
        llm_title.set_margin_bottom(6)
        vbox_right.pack_start(llm_title, False, False, 0)

        # Alias field
        alias_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        alias_lbl = Gtk.Label.new("模型别名:")
        alias_lbl.set_size_request(90, -1)
        alias_lbl.set_xalign(0)
        alias_entry = Gtk.Entry.new()
        alias_entry.set_placeholder_text("例如: DeepSeek-V3")
        alias_entry.set_hexpand(True)
        alias_hbox.pack_start(alias_lbl, False, False, 0)
        alias_hbox.pack_start(alias_entry, True, True, 0)
        vbox_right.pack_start(alias_hbox, False, False, 0)

        # Base URL field
        url_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        url_lbl = Gtk.Label.new("Base URL:")
        url_lbl.set_size_request(90, -1)
        url_lbl.set_xalign(0)
        base_url_entry = Gtk.Entry.new()
        base_url_entry.set_placeholder_text("例如: https://api.deepseek.com/v1")
        base_url_entry.set_hexpand(True)
        url_hbox.pack_start(url_lbl, False, False, 0)
        url_hbox.pack_start(base_url_entry, True, True, 0)
        vbox_right.pack_start(url_hbox, False, False, 0)

        # API Key field
        key_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        key_lbl = Gtk.Label.new("API Key:")
        key_lbl.set_size_request(90, -1)
        key_lbl.set_xalign(0)
        api_key_entry = Gtk.Entry.new()
        api_key_entry.set_visibility(False)
        api_key_entry.set_hexpand(True)

        show_key_btn = Gtk.Button.new_with_label("显示")
        def on_show_key_clicked(_btn):
            visible = api_key_entry.get_visibility()
            api_key_entry.set_visibility(not visible)
            show_key_btn.set_label("隐藏" if not visible else "显示")
        show_key_btn.connect("clicked", on_show_key_clicked)

        key_hbox.pack_start(key_lbl, False, False, 0)
        key_hbox.pack_start(api_key_entry, True, True, 0)
        key_hbox.pack_start(show_key_btn, False, False, 0)
        vbox_right.pack_start(key_hbox, False, False, 0)

        # Model ID/Name field
        model_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        model_lbl = Gtk.Label.new("Model Name:")
        model_lbl.set_size_request(90, -1)
        model_lbl.set_xalign(0)
        model_name_entry = Gtk.Entry.new()
        model_name_entry.set_placeholder_text("例如: deepseek-chat, mistral-tiny")
        model_name_entry.set_hexpand(True)
        model_hbox.pack_start(model_lbl, False, False, 0)
        model_hbox.pack_start(model_name_entry, True, True, 0)
        vbox_right.pack_start(model_hbox, False, False, 0)

        # Check button row: default model + title generation model
        check_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 16)
        default_check = Gtk.CheckButton.new_with_label("设为默认模型")
        title_check = Gtk.CheckButton.new_with_label("标题生成模型")
        check_hbox.pack_start(default_check, False, False, 0)
        check_hbox.pack_start(title_check, False, False, 0)
        default_check.set_margin_top(4)
        default_check.set_margin_bottom(4)

        # Inference parameters section
        params_frame = Gtk.Frame.new("推理参数")
        params_frame.set_margin_top(6)
        params_frame.set_margin_bottom(6)

        params_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)
        params_vbox.set_margin_start(8)
        params_vbox.set_margin_end(8)
        params_vbox.set_margin_top(8)
        params_vbox.set_margin_bottom(8)

        # Temperature
        temp_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        temp_lbl = Gtk.Label.new("Temperature:")
        temp_lbl.set_size_request(120, -1)
        temp_lbl.set_xalign(0)
        temperature_spin = Gtk.SpinButton.new_with_range(0.0, 1.0, 0.05)
        temperature_spin.set_digits(2)
        temperature_spin.set_hexpand(True)
        temp_hint = Gtk.Label.new("(0~1)")
        temp_hint.get_style_context().add_class("dim-label")
        temp_hbox.pack_start(temp_lbl, False, False, 0)
        temp_hbox.pack_start(temperature_spin, True, True, 0)
        temp_hbox.pack_start(temp_hint, False, False, 0)
        params_vbox.pack_start(temp_hbox, False, False, 0)

        # Max Tokens
        mt_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        mt_lbl = Gtk.Label.new("Max Tokens:")
        mt_lbl.set_size_request(120, -1)
        mt_lbl.set_xalign(0)
        max_tokens_spin = Gtk.SpinButton.new_with_range(1, 131072, 1)
        max_tokens_spin.set_digits(0)
        max_tokens_spin.set_hexpand(True)
        mt_hint = Gtk.Label.new("(1~131072)")
        mt_hint.get_style_context().add_class("dim-label")
        mt_hbox.pack_start(mt_lbl, False, False, 0)
        mt_hbox.pack_start(max_tokens_spin, True, True, 0)
        mt_hbox.pack_start(mt_hint, False, False, 0)
        params_vbox.pack_start(mt_hbox, False, False, 0)

        # Top P
        top_p_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        top_p_lbl = Gtk.Label.new("Top P:")
        top_p_lbl.set_size_request(120, -1)
        top_p_lbl.set_xalign(0)
        top_p_spin = Gtk.SpinButton.new_with_range(0.0, 1.0, 0.05)
        top_p_spin.set_digits(2)
        top_p_spin.set_hexpand(True)
        top_p_hint = Gtk.Label.new("(0~1)")
        top_p_hint.get_style_context().add_class("dim-label")
        top_p_hbox.pack_start(top_p_lbl, False, False, 0)
        top_p_hbox.pack_start(top_p_spin, True, True, 0)
        top_p_hbox.pack_start(top_p_hint, False, False, 0)
        params_vbox.pack_start(top_p_hbox, False, False, 0)

        params_frame.add(params_vbox)

        vbox_right.pack_start(check_hbox, False, False, 0)
        vbox_right.pack_start(params_frame, False, False, 0)

        # Actions box (Delete button)
        action_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        delete_model_btn = Gtk.Button.new_with_label("🗑️ 删除模型")
        action_hbox.pack_end(delete_model_btn, False, False, 0)
        vbox_right.pack_start(action_hbox, False, False, 4)

        note_lbl = Gtk.Label.new("注：敏感 API Key 会以 600 文件权限 safe 存储于本地。")
        note_lbl.set_xalign(0)
        note_lbl.set_line_wrap(True)
        note_lbl.get_style_context().add_class("dim-label")
        vbox_right.pack_start(note_lbl, False, False, 6)

        llm_edit_box.pack_start(vbox_right, True, True, 0)

        mid_vbox.pack_start(llm_edit_box, True, True, 0)
        llm_edit_box.show_all()
        llm_edit_box.set_no_show_all(True)
        llm_edit_box.hide()

        vbox.pack_start(mid_vbox, True, True, 0)

        updating_checks = [False]

        def update_select_all_state():
            if updating_checks[0]:
                return
            updating_checks[0] = True
            all_checked = text_check.get_active() and link_check.get_active() and code_check.get_active()
            select_all_check.set_active(all_checked)
            updating_checks[0] = False

        def on_select_all_toggled(widget):
            if updating_checks[0]:
                return
            updating_checks[0] = True
            active = widget.get_active()
            text_check.set_active(active)
            link_check.set_active(active)
            code_check.set_active(active)
            updating_checks[0] = False

        def on_check_toggled(widget):
            update_select_all_state()

        select_all_check.connect("toggled", on_select_all_toggled)
        text_check.connect("toggled", on_check_toggled)
        link_check.connect("toggled", on_check_toggled)
        code_check.connect("toggled", on_check_toggled)

        def get_selected_categories():
            cats = []
            if text_check.get_active():
                cats.append("text")
            if link_check.get_active():
                cats.append("link")
            if code_check.get_active():
                cats.append("code")
            return cats

        # Bottom buttons
        bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        bottom_box.set_margin_top(8)
        bottom_box.set_margin_bottom(8)
        bottom_box.set_margin_start(12)
        bottom_box.set_margin_end(12)

        delete_btn = Gtk.Button.new_with_label("🗑️ Delete")
        cancel_btn = Gtk.Button.new_with_label("Cancel")
        confirm_btn = Gtk.Button.new_with_label("Confirm")
        confirm_btn.get_style_context().add_class("suggested-action")

        bottom_box.pack_start(delete_btn, False, False, 0)
        bottom_box.pack_end(confirm_btn, False, False, 0)
        bottom_box.pack_end(cancel_btn, False, False, 0)
        vbox.pack_start(bottom_box, False, False, 0)

        sep2 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep2, False, False, 0)
        vbox.reorder_child(bottom_box, -1)

        def save_current_model_fields():
            if 0 <= self._active_model_idx < len(local_models):
                m = local_models[self._active_model_idx]
                m.alias = alias_entry.get_text().strip() or "Unnamed"
                m.base_url = base_url_entry.get_text().strip()
                m.api_key = api_key_entry.get_text().strip()
                m.model_name = model_name_entry.get_text().strip()
                m.is_default = default_check.get_active()
                m.is_title_model = title_check.get_active()
                m.temperature = temperature_spin.get_value()
                m.max_tokens = int(max_tokens_spin.get_value())
                m.top_p = top_p_spin.get_value()

        def _model_label(m):
            parts = []
            if m.is_default:
                parts.append("默认")
            if m.is_title_model:
                parts.append("标题")
            suffix = f" ({', '.join(parts)})" if parts else ""
            return m.alias + suffix

        def rebuild_model_list():
            self._updating_model_ui = True
            has_handler = self._model_row_selected_handler_id is not None
            if has_handler:
                model_list_box.handler_block(self._model_row_selected_handler_id)
            try:
                for child in model_list_box.get_children():
                    model_list_box.remove(child)
                for idx, m in enumerate(local_models):
                    row = Gtk.ListBoxRow.new()
                    row.idx = idx
                    label_text = _model_label(m)
                    lbl = Gtk.Label.new(label_text)
                    lbl.set_xalign(0)
                    lbl.set_margin_start(8)
                    lbl.set_margin_end(8)
                    lbl.set_margin_top(6)
                    lbl.set_margin_bottom(6)
                    row.add(lbl)
                    model_list_box.add(row)
                model_list_box.show_all()
                if 0 <= self._active_model_idx < len(local_models):
                    row = model_list_box.get_row_at_index(self._active_model_idx)
                    if row:
                        model_list_box.select_row(row)
            finally:
                if has_handler:
                    model_list_box.handler_unblock(self._model_row_selected_handler_id)
                self._updating_model_ui = False

        def load_model_to_fields(idx):
            if 0 <= idx < len(local_models):
                self._updating_model_ui = True
                m = local_models[idx]
                alias_entry.set_text(m.alias)
                base_url_entry.set_text(m.base_url)
                api_key_entry.set_text(m.api_key)
                model_name_entry.set_text(m.model_name)
                default_check.set_active(m.is_default)
                title_check.set_active(m.is_title_model)
                temperature_spin.set_value(m.temperature)
                max_tokens_spin.set_value(m.max_tokens)
                top_p_spin.set_value(m.top_p)
                self._updating_model_ui = False

                alias_entry.set_sensitive(True)
                base_url_entry.set_sensitive(True)
                api_key_entry.set_sensitive(True)
                model_name_entry.set_sensitive(True)
                default_check.set_sensitive(True)
                title_check.set_sensitive(True)
                temperature_spin.set_sensitive(True)
                max_tokens_spin.set_sensitive(True)
                top_p_spin.set_sensitive(True)
                delete_model_btn.set_sensitive(len(local_models) > 1)
            else:
                self._updating_model_ui = True
                alias_entry.set_text("")
                base_url_entry.set_text("")
                api_key_entry.set_text("")
                model_name_entry.set_text("")
                default_check.set_active(False)
                title_check.set_active(False)
                temperature_spin.set_value(DEFAULT_TEMPERATURE)
                max_tokens_spin.set_value(DEFAULT_MAX_TOKENS)
                top_p_spin.set_value(DEFAULT_TOP_P)
                self._updating_model_ui = False

                alias_entry.set_sensitive(False)
                base_url_entry.set_sensitive(False)
                api_key_entry.set_sensitive(False)
                model_name_entry.set_sensitive(False)
                default_check.set_sensitive(False)
                title_check.set_sensitive(False)
                temperature_spin.set_sensitive(False)
                max_tokens_spin.set_sensitive(False)
                top_p_spin.set_sensitive(False)
                delete_model_btn.set_sensitive(False)

        def on_model_row_selected(listbox, row):
            if self._updating_model_ui:
                return
            if not row or row.get_parent() != listbox:
                return
            if row.idx == self._active_model_idx:
                return
            save_current_model_fields()
            self._active_model_idx = row.idx
            load_model_to_fields(self._active_model_idx)

        def on_add_model_clicked(_btn):
            save_current_model_fields()
            new_m = LLMModelConfig(
                alias="New Model",
                base_url="https://api.deepseek.com/v1",
                api_key="",
                model_name="deepseek-chat",
                is_default=False,
                temperature=DEFAULT_TEMPERATURE,
                max_tokens=DEFAULT_MAX_TOKENS,
                top_p=DEFAULT_TOP_P,
            )
            local_models.append(new_m)
            self._active_model_idx = len(local_models) - 1
            rebuild_model_list()
            load_model_to_fields(self._active_model_idx)
            alias_entry.grab_focus()

        def on_delete_model_clicked(_btn):
            if len(local_models) <= 1:
                return
            confirm_dialog = Gtk.MessageDialog(
                transient_for=dialog,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="确认删除模型吗？",
            )
            confirm_dialog.format_secondary_text(f"模型 '{local_models[self._active_model_idx].alias}' 将被永久删除。")
            resp = confirm_dialog.run()
            confirm_dialog.destroy()
            if resp == Gtk.ResponseType.YES:
                was_default = local_models[self._active_model_idx].is_default
                local_models.pop(self._active_model_idx)
                self._active_model_idx = max(0, self._active_model_idx - 1)
                if was_default and local_models:
                    local_models[self._active_model_idx].is_default = True
                rebuild_model_list()
                load_model_to_fields(self._active_model_idx)

        def on_alias_entry_changed(entry):
            if self._updating_model_ui:
                return
            if 0 <= self._active_model_idx < len(local_models):
                alias_text = entry.get_text()
                local_models[self._active_model_idx].alias = alias_text
                row = model_list_box.get_row_at_index(self._active_model_idx)
                if row:
                    lbl = row.get_child()
                    if isinstance(lbl, Gtk.Label):
                        lbl.set_text(_model_label(local_models[self._active_model_idx]))

        def on_default_toggled(widget):
            if self._updating_model_ui:
                return
            if 0 <= self._active_model_idx < len(local_models):
                active = widget.get_active()
                if active:
                    for idx, m in enumerate(local_models):
                        m.is_default = (idx == self._active_model_idx)
                        row = model_list_box.get_row_at_index(idx)
                        if row:
                            lbl = row.get_child()
                            if isinstance(lbl, Gtk.Label):
                                lbl.set_text(_model_label(m))
                else:
                    has_other_default = any(m.is_default for idx, m in enumerate(local_models) if idx != self._active_model_idx)
                    if not has_other_default:
                        self._updating_model_ui = True
                        widget.set_active(True)
                        self._updating_model_ui = False

        def on_title_toggled(widget):
            if self._updating_model_ui:
                return
            if 0 <= self._active_model_idx < len(local_models):
                active = widget.get_active()
                if active:
                    for idx, m in enumerate(local_models):
                        m.is_title_model = (idx == self._active_model_idx)
                        row = model_list_box.get_row_at_index(idx)
                        if row:
                            lbl = row.get_child()
                            if isinstance(lbl, Gtk.Label):
                                lbl.set_text(_model_label(m))

        def refresh_model_combo():
            model_combo.remove_all()
            for m in self.llm_settings_store.models:
                display_text = f"{m.alias} (默认)" if m.is_default else m.alias
                model_combo.append(m.alias, display_text)

        self._model_row_selected_handler_id = model_list_box.connect("row-selected", on_model_row_selected)
        btn_add_model.connect("clicked", on_add_model_clicked)
        delete_model_btn.connect("clicked", on_delete_model_clicked)
        alias_entry.connect("changed", on_alias_entry_changed)
        default_check.connect("toggled", on_default_toggled)
        title_check.connect("toggled", on_title_toggled)

        refresh_model_combo()

        def save_current_active_prompt():
            if self._editing_global_settings:
                save_current_model_fields()
            elif 0 <= self._dialog_active_idx < len(prompts):
                name = name_entry.get_text().strip()
                prompts[self._dialog_active_idx].name = name if name else "New Prompt"
                buf = prompt_textview.get_buffer()
                start, end = buf.get_bounds()
                prompt_text = buf.get_text(start, end, False)
                prompts[self._dialog_active_idx].prompt = prompt_text
                prompts[self._dialog_active_idx].categories = get_selected_categories()
                prompts[self._dialog_active_idx].action_type = "api" if mode_api_radio.get_active() else "web"
                prompts[self._dialog_active_idx].bound_model_alias = model_combo.get_active_id()

        def load_prompt_to_fields(idx):
            if 0 <= idx < len(prompts):
                updating_checks[0] = True
                name_entry.handler_block(changed_handler_id)
                name_entry.set_text(prompts[idx].name)
                name_entry.handler_unblock(changed_handler_id)

                prompt_textview.get_buffer().set_text(prompts[idx].prompt)

                cats = getattr(prompts[idx], "categories", None) or ["text"]
                text_check.set_active("text" in cats)
                link_check.set_active("link" in cats)
                code_check.set_active("code" in cats)

                all_checked = "text" in cats and "link" in cats and "code" in cats
                select_all_check.set_active(all_checked)

                act_type = getattr(prompts[idx], "action_type", "web")
                if act_type == "api":
                    mode_api_radio.set_active(True)
                else:
                    mode_web_radio.set_active(True)

                bound_alias = getattr(prompts[idx], "bound_model_alias", None)
                if bound_alias and any(m.alias == bound_alias for m in self.llm_settings_store.models):
                    model_combo.set_active_id(bound_alias)
                else:
                    default_model = next((m for m in self.llm_settings_store.models if m.is_default), None)
                    if default_model:
                        model_combo.set_active_id(default_model.alias)
                    elif self.llm_settings_store.models:
                        model_combo.set_active_id(self.llm_settings_store.models[0].alias)

                updating_checks[0] = False

                name_entry.set_sensitive(True)
                prompt_textview.set_sensitive(True)
                insert_btn.set_sensitive(True)
                text_check.set_sensitive(True)
                link_check.set_sensitive(True)
                code_check.set_sensitive(True)
                select_all_check.set_sensitive(True)
                delete_btn.set_sensitive(True)
                mode_web_radio.set_sensitive(True)
                mode_api_radio.set_sensitive(True)
                model_combo.set_sensitive(mode_api_radio.get_active())
            else:
                updating_checks[0] = True
                name_entry.handler_block(changed_handler_id)
                name_entry.set_text("")
                name_entry.handler_unblock(changed_handler_id)

                prompt_textview.get_buffer().set_text("")
                text_check.set_active(False)
                link_check.set_active(False)
                code_check.set_active(False)
                select_all_check.set_active(False)
                updating_checks[0] = False

                name_entry.set_sensitive(False)
                prompt_textview.set_sensitive(False)
                insert_btn.set_sensitive(False)
                text_check.set_sensitive(False)
                link_check.set_sensitive(False)
                code_check.set_sensitive(False)
                select_all_check.set_sensitive(False)
                delete_btn.set_sensitive(False)
                mode_web_radio.set_sensitive(False)
                mode_api_radio.set_sensitive(False)
                model_combo.set_sensitive(False)

        def switch_to_prompt_edit_mode():
            if self._editing_global_settings:
                save_current_model_fields()
                self.llm_settings_store.models = deepcopy(local_models)
                self.llm_settings_store.save_all()
                refresh_model_combo()

                self._editing_global_settings = False
                settings_btn.get_style_context().remove_class("suggested-action")
                llm_edit_box.hide()
                prompt_edit_box.show()

        def rebuild_tabs():
            for child in tab_bar_box.get_children():
                tab_bar_box.remove(child)
            tab_buttons.clear()

            for idx, p in enumerate(prompts):
                btn = Gtk.Button.new_with_label(p.name)
                btn.idx = idx
                if idx == self._dialog_active_idx and not self._editing_global_settings:
                    btn.get_style_context().add_class("suggested-action")

                def on_tab_clicked(b):
                    nonlocal changed_handler_id
                    save_current_active_prompt()
                    switch_to_prompt_edit_mode()

                    self._dialog_active_idx = b.idx
                    rebuild_tabs()
                    load_prompt_to_fields(b.idx)

                btn.connect("clicked", on_tab_clicked)
                tab_bar_box.pack_start(btn, False, False, 0)
                tab_buttons[idx] = btn
            tab_bar_box.show_all()

        def on_add_clicked(_btn):
            save_current_active_prompt()
            switch_to_prompt_edit_mode()

            new_p = CustomPrompt(
                id=str(uuid4()),
                name="New Prompt",
                prompt="",
                categories=["text"],
                action_type="web"
            )
            prompts.append(new_p)
            self._dialog_active_idx = len(prompts) - 1
            rebuild_tabs()
            load_prompt_to_fields(self._dialog_active_idx)
            name_entry.grab_focus()

        def on_settings_clicked(_btn):
            if self._editing_global_settings:
                return
            save_current_active_prompt()
            self._editing_global_settings = True

            for b in tab_buttons.values():
                b.get_style_context().remove_class("suggested-action")
            settings_btn.get_style_context().add_class("suggested-action")

            prompt_edit_box.hide()
            llm_edit_box.show()

            nonlocal local_models
            local_models = deepcopy(self.llm_settings_store.models)
            
            self._active_model_idx = 0
            for idx, m in enumerate(local_models):
                if m.is_default:
                    self._active_model_idx = idx
                    break
            
            rebuild_model_list()
            load_model_to_fields(self._active_model_idx)

            delete_btn.set_sensitive(False)

        settings_btn.connect("clicked", on_settings_clicked)

        def on_delete_clicked(_btn):
            if self._editing_global_settings:
                return
            if not (0 <= self._dialog_active_idx < len(prompts)):
                return

            confirm = Gtk.MessageDialog(
                transient_for=dialog,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="确定要删除该提示词配置吗？",
            )

            def on_confirm_resp(dlg, resp):
                dlg.destroy()
                if resp == Gtk.ResponseType.YES:
                    prompts.pop(self._dialog_active_idx)
                    if not prompts:
                        self._dialog_active_idx = -1
                    else:
                        self._dialog_active_idx = max(0, self._dialog_active_idx - 1)
                    rebuild_tabs()
                    load_prompt_to_fields(self._dialog_active_idx)

            confirm.connect("response", on_confirm_resp)
            confirm.show_all()

        def on_confirm_clicked(_btn):
            save_current_active_prompt()

            if self._editing_global_settings:
                self.llm_settings_store.models = deepcopy(local_models)
                self.llm_settings_store.save_all()

            if not self._editing_global_settings and 0 <= self._dialog_active_idx < len(prompts):
                cats = get_selected_categories()
                if not cats:
                    warning = Gtk.MessageDialog(
                        transient_for=dialog,
                        modal=True,
                        message_type=Gtk.MessageType.WARNING,
                        buttons=Gtk.ButtonsType.OK,
                        text="配置无效",
                    )
                    warning.format_secondary_text("请至少勾选一个适用类别（文本、链接、代码）。")

                    def on_warn_resp(dlg, resp):
                        dlg.destroy()
                    warning.connect("response", on_warn_resp)
                    warning.show_all()
                    return

            for p in prompts:
                if not p.name.strip():
                    p.name = "New Prompt"
                if not getattr(p, "categories", None):
                    p.categories = ["text"]
                if not getattr(p, "action_type", None):
                    p.action_type = "web"
            self.custom_prompts_store.save_all(prompts)
            dialog.destroy()

        def on_name_changed(entry):
            idx = self._dialog_active_idx
            if 0 <= idx < len(prompts) and not self._editing_global_settings:
                new_text = entry.get_text().strip()
                display_name = new_text if new_text else "New Prompt"
                prompts[idx].name = display_name
                if idx in tab_buttons:
                    tab_buttons[idx].set_label(display_name)

        changed_handler_id = name_entry.connect("changed", on_name_changed)

        add_btn.connect("clicked", on_add_clicked)
        delete_btn.connect("clicked", on_delete_clicked)
        cancel_btn.connect("clicked", lambda _: dialog.destroy())
        confirm_btn.connect("clicked", on_confirm_clicked)

        rebuild_tabs()
        load_prompt_to_fields(self._dialog_active_idx)

        dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
        dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())

        dialog.show_all()


def show_prompts_config_dialog(parent_window, custom_prompts_store, llm_settings_store,
                               on_dialog_shown, on_dialog_hidden):
    PromptsConfigDialog(parent_window, custom_prompts_store, llm_settings_store,
                        on_dialog_shown, on_dialog_hidden)

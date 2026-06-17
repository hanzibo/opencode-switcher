#!/usr/bin/env python3
import os
import json
import re

CONFIG_DIR = os.path.expanduser("~/.config/opencode-switcher")
CLIPBOARD_PATH = os.path.join(CONFIG_DIR, "clipboard_history.json")

from typing import Optional
from clipboard_store import classify_text, detect_language_name

def run_migration():
    if not os.path.isfile(CLIPBOARD_PATH):
        return
        
    try:
        with open(CLIPBOARD_PATH, "r") as f:
            items = json.load(f)
    except Exception as e:
        print(f"Error loading history during migration: {e}")
        return
        
    if not isinstance(items, list):
        return
        
    updated = False
    for item in items:
        if not isinstance(item, dict):
            continue
        # If type is missing, or is "text" (which needs classification check)
        current_type = item.get("type", "text")
        current_lang = item.get("language")
        text = item.get("text", "")
        # Preserve "image" type
        if current_type == "image":
            continue
        if text == "[Image]":
            item["type"] = "image"
            updated = True
            continue
            
        new_type = classify_text(text)
        new_lang = detect_language_name(text) if new_type == "code" else None
        
        if new_type != current_type or new_lang != current_lang:
            item["type"] = new_type
            item["language"] = new_lang
            updated = True
                
    if updated:
        try:
            with open(CLIPBOARD_PATH, "w") as f:
                json.dump(items, f, indent=2)
            print("Successfully migrated clipboard history item types.")
        except Exception as e:
            print(f"Error writing migrated history: {e}")

if __name__ == "__main__":
    run_migration()

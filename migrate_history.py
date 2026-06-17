#!/usr/bin/env python3
import os
import json
import re

CONFIG_DIR = os.path.expanduser("~/.config/opencode-switcher")
CLIPBOARD_PATH = os.path.join(CONFIG_DIR, "clipboard_history.json")

def classify_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("http"):
        return "link"
    
    # Check JS/Gjs keywords: const, function, export
    # Check curly braces with newline: {\s*\n or \n\s*}
    has_keywords = bool(re.search(r'\b(const|function|export)\b', text))
    has_curly_newline = bool(re.search(r'[\{\}]\s*[\r\n]|[\r\n]\s*[\{\}]', text))
    if has_keywords or has_curly_newline:
        return "code"
        
    return "text"

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
        text = item.get("text", "")
        # Preserve "image" type
        if current_type == "image":
            continue
        if text == "[Image]":
            item["type"] = "image"
            updated = True
            continue
            
        new_type = classify_text(text)
        if new_type != current_type:
            item["type"] = new_type
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

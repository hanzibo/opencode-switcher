"""Regression tests for missing typing/runtime imports in P0 hardening modules.

Ensures ``typing.get_type_hints`` can resolve every annotation in the
affected modules — guards against a missing ``Any`` import in
``stores/skill_store``, a missing ``Optional`` import in
``tool_registry``, and unresolved ``mcp`` SDK types used by the
deprecated functions in ``mcp_integration/tool_adapter``.
"""

import typing
import unittest

import stores.skill_store
import tool_registry
from mcp_integration import tool_adapter


class TestTypingImports(unittest.TestCase):
    """Exercise the affected annotations via ``typing.get_type_hints``."""

    def test_skill_store_annotations_resolve(self):
        """``Any`` in SkillStore.__init__ must resolve at runtime."""
        init_hints = typing.get_type_hints(stores.skill_store.SkillStore.__init__)
        self.assertIn("global_dir", init_hints)
        self.assertIn("disabled_skills", init_hints)

    def test_tool_registry_annotations_resolve(self):
        """``Optional`` in tool_registry public functions must resolve."""
        hints = typing.get_type_hints(tool_registry.get_enabled_tool_definitions)
        self.assertIn("disabled_list", hints)
        hints = typing.get_type_hints(tool_registry.is_tool_disabled)
        self.assertIn("disabled_list", hints)
        hints = typing.get_type_hints(tool_registry.execute_tool_call)
        self.assertIn("disabled_list", hints)

    def test_tool_adapter_deprecated_annotations_resolve(self):
        """Deprecated functions must keep ``mcp`` SDK types resolvable."""
        hints = typing.get_type_hints(tool_adapter.tool_result_to_text)
        self.assertIn("result", hints)
        hints = typing.get_type_hints(tool_adapter.merge_mcp_tools_into_definitions)
        self.assertIn("mcp_tools", hints)
        self.assertIn("existing_defs", hints)


if __name__ == "__main__":
    unittest.main()

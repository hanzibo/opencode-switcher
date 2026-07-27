"""Unit tests for SkillStore and read_skill tool.
"""

import os
import shutil
import tempfile
import unittest

from skill_store import SkillStore, _parse_frontmatter, _parse_skill_file
from tool_registry.skill import execute_read_skill


class TestSkillStore(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.global_skills = os.path.join(self.tmp_dir, "global_skills")
        self.proj_dir = os.path.join(self.tmp_dir, "project")
        self.proj_skills = os.path.join(self.proj_dir, ".opencode", "skills")

        os.makedirs(self.global_skills, exist_ok=True)
        os.makedirs(self.proj_skills, exist_ok=True)

        # Create global skill: code-review
        global_skill_dir = os.path.join(self.global_skills, "code-review")
        os.makedirs(global_skill_dir, exist_ok=True)
        with open(os.path.join(global_skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("""---
name: code-review
description: Perform code reviews on repository code.
allowed-tools: read_file, grep_search
---

# Code Review Skill

1. Check formatting.
2. Check security vulnerabilities.
""")

        # Create project skill: deploy-app
        proj_skill_dir = os.path.join(self.proj_skills, "deploy-app")
        os.makedirs(proj_skill_dir, exist_ok=True)
        with open(os.path.join(proj_skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("""---
name: deploy-app
description: Deployment workflow for app.
---

# Deploy App Skill

Run `./install.sh install` and verify service.
""")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_parse_frontmatter(self):
        content = """---
name: test-skill
description: Test description
allowed-tools: read_file, bash
---

# Instructions
Do work.
"""
        meta, body = _parse_frontmatter(content)
        self.assertEqual(meta.get("name"), "test-skill")
        self.assertEqual(meta.get("description"), "Test description")
        self.assertTrue("# Instructions" in body)

    def test_skill_store_discovery(self):
        store = SkillStore(global_dir=self.global_skills)
        skills = store.get_skills(cwd=self.proj_dir)

        names = {s.name for s in skills}
        self.assertIn("code-review", names)
        self.assertIn("deploy-app", names)

    def test_prompt_summary(self):
        store = SkillStore(global_dir=self.global_skills)
        summary = store.get_skills_prompt_summary(cwd=self.proj_dir)

        self.assertIn("<available_skills>", summary)
        self.assertIn("<instructions>", summary)
        self.assertIn("<name>code-review</name>", summary)
        self.assertIn("<location>global</location>", summary)
        self.assertIn("<allowed_tools>", summary)
        self.assertIn("<name>deploy-app</name>", summary)
        self.assertIn("<location>project</location>", summary)

    def test_get_skill_content(self):
        store = SkillStore(global_dir=self.global_skills)
        content = store.get_skill_content("code-review", cwd=self.proj_dir)

        self.assertIsNotNone(content)
        self.assertIn("Check formatting", content)

    def test_get_skill_detail(self):
        store = SkillStore(global_dir=self.global_skills)
        detail = store.get_skill_detail("deploy-app", cwd=self.proj_dir)

        self.assertIsNotNone(detail)
        path, body, sk = detail
        self.assertTrue(path.endswith("SKILL.md"))
        self.assertIn("Deploy App Skill", body)
        self.assertEqual(sk.name, "deploy-app")
        self.assertEqual(sk.description, "Deployment workflow for app.")
        self.assertTrue(sk.path.endswith("SKILL.md"))

    def test_frontmatter_extended_fields(self):
        skill_dir = os.path.join(self.tmp_dir, "ext_skill")
        os.makedirs(skill_dir, exist_ok=True)
        skill_file = os.path.join(skill_dir, "SKILL.md")
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(
                "---\n"
                "name: ext-skill\n"
                "description: Extended skill\n"
                "license: MIT\n"
                "compatibility: python >= 3.10\n"
                "custom_key: custom_val\n"
                "---\n"
                "# Extended Skill\n"
            )
        store = SkillStore(global_dir=skill_dir)
        skills = store.get_skills()
        self.assertEqual(len(skills), 1)
        sk = skills[0]
        self.assertEqual(sk.license, "MIT")
        self.assertEqual(sk.compatibility, "python >= 3.10")
        self.assertEqual(sk.metadata.get("custom_key"), "custom_val")

    def test_skill_subdirectory_resources(self):
        skill_dir = os.path.join(self.tmp_dir, "res_skill")
        os.makedirs(os.path.join(skill_dir, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(skill_dir, "references"), exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: res-skill\ndescription: Resource skill\n---\n")
        with open(os.path.join(skill_dir, "scripts", "check.py"), "w", encoding="utf-8") as f:
            f.write("print('hello')\n")
        with open(os.path.join(skill_dir, "references", "guide.md"), "w", encoding="utf-8") as f:
            f.write("# Guide\n")

        store = SkillStore(global_dir=skill_dir)
        skills = store.get_skills()
        sk = skills[0]
        self.assertIn("scripts", sk.resources)
        self.assertIn("scripts/check.py", sk.resources["scripts"])
        self.assertIn("references", sk.resources)
        self.assertIn("references/guide.md", sk.resources["references"])

    def test_multiple_global_directories(self):
        global_2 = os.path.join(self.tmp_dir, "global_skills_2")
        os.makedirs(os.path.join(global_2, "extra-skill"), exist_ok=True)
        with open(os.path.join(global_2, "extra-skill", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: extra-skill\ndescription: Extra skill\n---\n")

        store = SkillStore(global_dir=[self.global_skills, global_2])
        skills = store.get_skills()
        names = {s.name for s in skills}
        self.assertIn("code-review", names)
        self.assertIn("extra-skill", names)

    def test_enable_global_skills_toggle(self):
        store_disabled = SkillStore(global_dir=self.global_skills, enable_global_skills=False)
        skills = store_disabled.get_skills(cwd=self.proj_dir)
        names = {s.name for s in skills}

        self.assertNotIn("code-review", names)
        self.assertIn("deploy-app", names)

    def test_individual_disabled_skills(self):
        store = SkillStore(global_dir=self.global_skills, disabled_skills=["code-review"])
        skills = store.get_skills(cwd=self.proj_dir)
        names = {s.name for s in skills}

        self.assertNotIn("code-review", names)
        self.assertIn("deploy-app", names)


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from tool_registry._state import bash as _bash_state
from tool_registry.bash import execute_bash, get_bash_cwd, set_bash_cwd, close_bash_session
from skill_store import SkillStore

class TestBashIsolation(unittest.TestCase):
    def setUp(self):
        self.test_dir_a = os.path.join(os.getcwd(), "tests")
        self.test_dir_b = "/tmp"

    def test_multi_session_cwd_isolation(self):
        """测试多会话之间 CWD 的正确隔离。"""
        session_a = "conv_test_a"
        session_b = "conv_test_b"

        set_bash_cwd(self.test_dir_a, session_key=session_a)
        set_bash_cwd(self.test_dir_b, session_key=session_b)

        self.assertEqual(get_bash_cwd(session_key=session_a), self.test_dir_a)
        self.assertEqual(get_bash_cwd(session_key=session_b), self.test_dir_b)

    def test_cwd_inheritance_on_new_conversation(self):
        """测试新建会话时自动继承上一个会话的 CWD。"""
        old_session = "conv_old"
        new_session = "conv_new"

        target_cwd = "/home/hzb/Work/Test/todo-app" if os.path.exists("/home/hzb/Work/Test/todo-app") else self.test_dir_a
        set_bash_cwd(target_cwd, session_key=old_session)

        # 模拟新建会话时继承
        inherited_cwd = get_bash_cwd(session_key=old_session)
        set_bash_cwd(inherited_cwd, session_key=new_session)

        self.assertEqual(get_bash_cwd(session_key=new_session), target_cwd)

    def test_session_cleanup_on_delete(self):
        """测试删除会话时正确释放子进程资源。"""
        sess_key = "conv_to_delete"
        set_bash_cwd(self.test_dir_a, session_key=sess_key)
        
        # 触发生成 session
        _bash_state._sessions[sess_key] = type("DummySession", (), {"stop": lambda self: None})()
        self.assertIn(sess_key, _bash_state._sessions)

        close_bash_session(sess_key)
        self.assertNotIn(sess_key, _bash_state._sessions)

    def test_skill_discovery_with_explicit_session_key(self):
        """测试指定 session_key 时 SkillStore 能够准确定位关联目录。"""
        sess_key = "conv_todo"
        target_dir = "/home/hzb/Work/Test/todo-app"
        if not os.path.exists(target_dir):
            self.skipTest(f"{target_dir} 不存在，跳过具体文件扫描测试")

        set_bash_cwd(target_dir, session_key=sess_key)
        cwd = get_bash_cwd(session_key=sess_key)

        skills = SkillStore().get_skills(cwd=cwd)
        names = [s.name for s in skills]
        self.assertIn("hello-helper", names)

if __name__ == "__main__":
    unittest.main()

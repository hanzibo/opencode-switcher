import os
import time
import unittest
from tool_registry._state import bash as _bash_state
from tool_registry.bash import (
    execute_bash,
    get_bash_cwd,
    set_bash_cwd,
    close_bash_session,
    _detect_prompt_pattern,
    _format_stdin_stuck_message,
    _BashSession,
    _STDIN_IDLE_THRESHOLD,
)
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

    # ── Stdin detection tests ───────────────────────────────────────

    def test_detect_prompt_pattern_password(self):
        """检测密码提示模式。"""
        msg = _detect_prompt_pattern("Please enter your password:")
        self.assertIsNotNone(msg)
        self.assertIn("密码输入提示", msg)

    def test_detect_prompt_pattern_yesno(self):
        """检测 Yes/No 确认模式。"""
        msg = _detect_prompt_pattern("Continue? Yes/No")
        self.assertIsNotNone(msg)
        self.assertIn("Yes/No 确认", msg)

    def test_detect_prompt_pattern_no_false_positive(self):
        """普通输出不应误判为交互提示。"""
        msg = _detect_prompt_pattern("ls -la\ntotal 42\ndrwxr-xr-x")
        self.assertIsNone(msg)

    def test_detect_prompt_pattern_chinese(self):
        """检测中文提示模式。"""
        msg = _detect_prompt_pattern("确认？")
        self.assertIsNotNone(msg)
        self.assertIn("中文确认提示", msg)

    def test_format_stdin_stuck_message(self):
        """验证 stdin 阻塞信息格式包含关键要素。"""
        msg = _format_stdin_stuck_message(
            "python3 << 'EOF'\nprint('hi')",
            tried_eof=True,
            tried_sigint=True,
        )
        self.assertIn("stdin 阻塞", msg)
        self.assertIn("SIGINT", msg)
        self.assertIn("heredoc", msg)
        self.assertIn("EOF", msg)

    def test_bash_session_send_eof_and_sigint(self):
        """验证 send_eof 和 send_sigint 方法不抛出异常。"""
        session = _BashSession()
        try:
            session.start()
            # send_eof 不应抛异常
            session.send_eof()
            # session 进程已结束，send_sigint 不应抛异常
            session.send_sigint()
        finally:
            session.stop()

    def test_eof_unblocks_hung_heredoc(self):
        """EOF 能够解阻塞卡在 heredoc 的命令（无需等待完整超时）。"""
        # 构造一个有问题的 heredoc（缺少结束标记）
        # 由于 close stdin 后 bash 会收到 EOF，应该很快结束
        result = execute_bash(
            'cat << EOF\ntest line\necho "should not reach here"',
            timeout=60,
        )
        # 应该返回（而不是等到 60s 超时），且包含阻塞提示
        self.assertNotIn("命令执行超时", result)
        self.assertIn("stdin 阻塞", result)

    def test_session_reusable_after_stdin_unblock(self):
        """session 在 stdin 解阻塞后仍可正常使用。"""
        # 先执行一个卡 stdin 的命令
        result1 = execute_bash(
            'cat << UNMATCHED\ndata',
            timeout=60,
        )
        # 再执行一个正常命令
        result2 = execute_bash('echo "still alive"')
        self.assertIn("still alive", result2)

    def test_normal_command_not_affected(self):
        """正常命令不应触发 stdin 检测。"""
        result = execute_bash('echo "hello world"')
        self.assertIn("hello world", result)
        self.assertNotIn("stdin 阻塞", result)

    def test_fast_command_completes_normally(self):
        """短命令正常完成，不被 idle 检测干扰。"""
        result = execute_bash('sleep 2 && echo "done"', timeout=30)
        self.assertIn("done", result)
        self.assertNotIn("stdin 阻塞", result)


if __name__ == "__main__":
    unittest.main()

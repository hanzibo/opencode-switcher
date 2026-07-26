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
    _check_heredoc,
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
        """不完整 heredoc 被预检直接拦截，无需等到执行阶段。"""
        result = execute_bash(
            'cat << EOF\ntest line\necho "should not reach here"',
            timeout=60,
        )
        # 预检立即拦截，不等到 idle 检测
        self.assertIn("不完整的 heredoc", result)
        self.assertNotIn("stdin 阻塞", result)

    def test_session_reusable_after_stdin_unblock(self):
        """被预检拦截后 session 仍可正常使用。"""
        # 预检拦截不完整的 heredoc，不涉及 session 操作
        result1 = execute_bash(
            'cat << UNMATCHED\ndata',
            timeout=60,
        )
        self.assertIn("不完整的 heredoc", result1)
        # 再执行一个正常命令，session 应正常工作
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

    # ── Heredoc pre-check tests ────────────────────────────────────

    def test_heredoc_complete(self):
        """完整 heredoc 不应报错。"""
        self.assertIsNone(_check_heredoc("cat << EOF\nhello\nEOF"))
        self.assertIsNone(_check_heredoc("cat << 'EOF'\nhello\nEOF"))
        self.assertIsNone(_check_heredoc("cat <<- EOF\n\thello\n\tEOF"))

    def test_heredoc_incomplete(self):
        """不完整 heredoc 应检测并报错。"""
        err = _check_heredoc("cat << EOF\nhello")
        self.assertIsNotNone(err)
        self.assertIn("EOF", err)

    def test_heredoc_multiple_complete(self):
        """多个 heredoc 全部完整不应报错。"""
        cmd = (
            "cat << EOF\nhello\nEOF\n"
            "cat << END\nworld\nEND"
        )
        self.assertIsNone(_check_heredoc(cmd))

    def test_heredoc_multiple_incomplete(self):
        """多个 heredoc 中有一个不完整应报错。"""
        cmd = (
            "cat << EOF\nhello\nEOF\n"
            "cat << END\nworld"
        )
        err = _check_heredoc(cmd)
        self.assertIsNotNone(err)
        self.assertIn("END", err)

    def test_heredoc_no_heredoc(self):
        """没有 heredoc 的命令不应误报。"""
        self.assertIsNone(_check_heredoc("echo hello"))
        self.assertIsNone(_check_heredoc("ls -la | grep foo"))
        self.assertIsNone(_check_heredoc("python3 -c 'print(1)'"))

    def test_heredoc_precheck_blocks_early(self):
        """execute_bash 应通过 heredoc 预检直接拦截，无需等待 idle 超时。"""
        result = execute_bash('cat << EOF\nhello\n', timeout=60)
        # 预检应立即返回错误，而非等 15s idle 检测
        self.assertIn("不完整的 heredoc", result)
        self.assertNotIn("stdin 阻塞", result)


if __name__ == "__main__":
    unittest.main()

import unittest

from message_format import render_markdown


class MarkdownRenderTests(unittest.TestCase):
    def test_headings_lists_emphasis_and_rules_are_rendered_without_markers(self):
        source = "## **训练安排**\n---\n- **深蹲**：循序渐进\n1. `每周三次`\n普通段落。"
        rendered, spans = render_markdown(source)
        self.assertEqual(rendered, "训练安排\n\n• 深蹲：循序渐进\n1. 每周三次\n普通段落。")
        self.assertNotIn("##", rendered)
        self.assertNotIn("**", rendered)
        self.assertNotIn("`", rendered)
        self.assertEqual([name for name, _, _ in spans], ["strong", "heading_3", "strong", "inline_code"])

    def test_unclosed_inline_markdown_remains_literal(self):
        rendered, spans = render_markdown("正在输出 **未完成")
        self.assertEqual(rendered, "正在输出 **未完成")
        self.assertEqual(spans, [])


if __name__ == "__main__":
    unittest.main()

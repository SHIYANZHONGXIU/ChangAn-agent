"""识别代码围栏时保留原始缩进、空行和换行，不修改复制内容。"""

import re


CODE_FENCES = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n(.*?)^[ \t]{0,3}\1[ \t]*(?:\r?\n|$)", re.MULTILINE | re.DOTALL)


def render_markdown(text):
    """Return readable text and style spans for the Markdown used in model replies."""
    output = []
    spans = []
    offset = 0
    inline = re.compile(r"(\*\*[^*\n]+\*\*|(?<!\*)\*[^*\n]+\*|`[^`\n]+`)")

    for line in str(text or "").splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = "\n" if line.endswith(("\n", "\r")) else ""
        if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", body):
            output.append("\n" if newline else "")
            offset += 1 if newline else 0
            continue

        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", body)
        if heading:
            body = heading.group(2)
            heading_size = 5 if len(heading.group(1)) == 1 else 3 if len(heading.group(1)) == 2 else 1
        else:
            heading_size = 0
            body = re.sub(r"^\s{0,3}>\s?", "", body)
            body = re.sub(r"^(\s*)[-+*]\s+", r"\1• ", body)

        line_start = offset
        cursor = 0
        rendered = []
        for match in inline.finditer(body):
            rendered.append(body[cursor:match.start()])
            token = match.group()
            if token.startswith("**"):
                name, value = "strong", token[2:-2]
            elif token.startswith("*"):
                name, value = "emphasis", token[1:-1]
            else:
                name, value = "inline_code", token[1:-1]
            start = line_start + sum(map(len, rendered))
            rendered.append(value)
            spans.append((name, start, start + len(value)))
            cursor = match.end()
        rendered.append(body[cursor:])
        rendered_text = "".join(rendered)
        output.append(rendered_text + newline)
        offset += len(rendered_text) + len(newline)
        if heading_size and rendered_text:
            spans.append((f"heading_{heading_size}", line_start, line_start + len(rendered_text)))

    return "".join(output), spans

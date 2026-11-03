"""识别代码围栏时保留原始缩进、空行和换行，不修改复制内容。"""

import re


CODE_FENCES = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n(.*?)^[ \t]{0,3}\1[ \t]*(?:\r?\n|$)", re.MULTILINE | re.DOTALL)

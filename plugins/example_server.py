"""长安自带的只读 MCP 示例，可独立运行，不需要模型密钥。"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("长安演示插件")


@mcp.tool()
def add(a: float, b: float) -> float:
    """计算两个数字之和，不读写文件。"""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """原样返回文本，用于验证连接。"""
    return text[:10000]


@mcp.resource("changan://welcome")
def welcome() -> str:
    """返回插件说明。"""
    return "长安 MCP 已连接：此示例仅计算数字和返回文本。"


@mcp.prompt()
def explain(topic: str) -> str:
    """演示一个带参数的提示模板。"""
    return f"请用简体中文解释：{topic}"


if __name__ == "__main__":
    mcp.run(transport="stdio")

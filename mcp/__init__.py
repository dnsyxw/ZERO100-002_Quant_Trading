"""MCP (Model Context Protocol) 服务端 —— 把本项目能力暴露给 AI Agent。

- `mcp_stdio.py`  : 零依赖的 stdio + JSON-RPC 2.0 服务端框架
- `futu_server.py`: 富途 OpenAPI 工具集(行情/交易/账户), 由 DSH 的 dsh-mcp-client 拉起

注意: 本目录名 `mcp` 与 PyPI 上的官方 `mcp` 包同名。本项目 **不使用** 官方 SDK,
`futu_server.py` 通过把自身所在目录显式插入 `sys.path` 来导入 `mcp_stdio`, 因此
既支持 `python mcp/futu_server.py` 直接运行, 也支持 `import mcp.futu_server`。

运行::

    python mcp/futu_server.py --list-tools
    python mcp/futu_server.py --selftest
"""

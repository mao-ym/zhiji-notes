"""写作陪伴空间的命令行入口。"""

import argparse
import json
from ipaddress import ip_address
from pathlib import Path
import socket
import sys

from writing_companion.app import ensure_loopback_host
from writing_companion.config import Settings
from writing_companion.services.recovery import RecoveryService
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""

    parser = argparse.ArgumentParser(prog="writing_companion")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="启动本地写作服务")
    _add_library_argument(serve)
    serve.add_argument("--host", type=_loopback_host, default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--lan", action="store_true", help="允许同一局域网设备访问手机页面")

    rebuild = commands.add_parser("rebuild-index", help="扫描 Markdown 并重建索引")
    _add_library_argument(rebuild)
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行 CLI 命令并返回进程退出码。"""

    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)

    try:
        if arguments.command == "serve":
            return _serve(arguments)
        if arguments.command == "rebuild-index":
            return _rebuild_index(arguments.library)
    except Exception as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    parser.error("未知命令")
    return 2


def _serve(arguments: argparse.Namespace) -> int:
    """启动 Uvicorn 本地服务。"""

    import uvicorn

    settings = Settings(
        library_root=arguments.library,
        host="0.0.0.0" if arguments.lan else arguments.host,
        port=arguments.port,
        lan_enabled=arguments.lan,
    )
    from writing_companion.app import create_app

    if arguments.lan:
        addresses = discover_private_ipv4_addresses()
        if addresses:
            for address in addresses:
                print(f"手机端: http://{address}:{settings.port}/mobile/")
        else:
            print("未发现私有 IPv4 地址，请使用 ipconfig 查找电脑地址。")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    return 0


def _rebuild_index(library_root: Path) -> int:
    """初始化指定写作库并重建 Markdown/FTS 索引。"""

    library = WritingLibrary(library_root)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    report = RecoveryService(library, database).rebuild_index()
    print(json.dumps(report.__dict__, ensure_ascii=False))
    return 0


def _add_library_argument(parser: argparse.ArgumentParser) -> None:
    """为子命令添加用户明确指定的写作库路径。"""

    parser.add_argument("--library", type=Path, required=True)


def _loopback_host(value: str) -> str:
    """校验 CLI host 只允许绑定本机 loopback 地址。"""

    try:
        ensure_loopback_host(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def discover_private_ipv4_addresses() -> list[str]:
    """发现本机可供同一局域网访问的私有 IPv4 地址。"""

    addresses: set[str] = set()
    try:
        entries = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    for entry in entries:
        address = entry[4][0]
        try:
            parsed = ip_address(address)
        except ValueError:
            continue
        if parsed.version == 4 and parsed.is_private and not parsed.is_loopback:
            addresses.add(address)
    return sorted(addresses)


if __name__ == "__main__":
    raise SystemExit(main())
